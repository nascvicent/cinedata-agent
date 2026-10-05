"""Avaliação do agente contra respostas de referência.

    python -m avaliacao                      # só valida as referências: 0 requisições
    python -m avaliacao --executar           # roda os casos pendentes, até 10 chamadas
    python -m avaliacao --executar --orcamento 6 --ids populares_top5,fora_do_escopo
    python -m avaliacao --executar --refazer # roda de novo casos já avaliados

Os resultados ficam em `avaliacao/resultados.json` e `avaliacao/relatorio.md`. Cada
execução continua de onde a anterior parou, então dá para espalhar a avaliação por
vários dias sem repetir chamadas.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from app.agent import OPENROUTER_BASE_URL, criar_agente, perguntar
from app.config import Settings, get_settings
from app.database import executar_select
from avaliacao.casos import CASOS, Caso
from avaliacao.comparar import Veredito, avaliar_ranking, avaliar_tabela, texto_menciona

PASTA = Path(__file__).parent
RESULTADOS = PASTA / "resultados.json"
RELATORIO = PASTA / "relatorio.md"
LINHAS_REFERENCIA = 200


def rodar_referencias(caso: Caso, settings: Settings) -> list[dict[str, Any]]:
    saida = []
    for sql in caso.referencias:
        inicio = time.monotonic()
        r = executar_select(settings.caminho_banco, sql, LINHAS_REFERENCIA, timeout=300)
        saida.append({**r, "sql": sql, "segundos": time.monotonic() - inicio})
    return saida


# ---------------------------------------------------------------- validação (grátis)


def validar(settings: Settings) -> None:
    print(f"Validando {len(CASOS)} casos direto no banco (nenhuma chamada ao modelo).\n")
    for caso in CASOS:
        print(f"[{caso.id}] {caso.pergunta}")
        if caso.tipo == "recusa":
            print("    recusa: sem referência SQL\n")
            continue
        for i, ref in enumerate(rodar_referencias(caso, settings), 1):
            alerta = ""
            if ref["segundos"] > settings.timeout_sql_segundos:
                alerta = f"  << passa do timeout do agente ({settings.timeout_sql_segundos:.0f}s)"
            print(f"    ref {i}: {len(ref['linhas'])} linhas, {ref['segundos']:.1f}s{alerta}")
            for linha in ref["linhas"][: min(caso.top, 3) if caso.tipo == "ranking" else 3]:
                print(f"        {linha}")
            if caso.tipo == "ranking" and ref["truncado"]:
                n = min(caso.top, len(ref["linhas"]))
                if ref["linhas"][-1][caso.metrica] == ref["linhas"][n - 1][caso.metrica]:
                    print(f"        aviso: empate na posição {n} passa de {LINHAS_REFERENCIA} linhas")
        print()
    max_req = settings.max_requisicoes_por_pergunta
    print(
        f"Custo estimado para rodar tudo: ~{2 * len(CASOS)} chamadas "
        f"(pior caso {max_req * len(CASOS)}). Use --executar com --orcamento para dividir."
    )


# ---------------------------------------------------------------- execução (gasta cota)


def consultar_cota(settings: Settings) -> dict[str, Any] | None:
    try:
        r = httpx.get(
            f"{OPENROUTER_BASE_URL}/key",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            timeout=10,
        )
        corpo = r.json().get("data", {})
        return corpo.get("free_model_daily_requests") or None
    except (httpx.HTTPError, ValueError):
        return None


def julgar(caso: Caso, consultas: list[dict[str, Any]], resposta: str,
           referencias: list[dict[str, Any]], linhas_antes: int, linhas_depois: int) -> dict:
    sucesso = [c for c in consultas if not c.get("erro")]
    if caso.tipo == "recusa":
        if linhas_antes != linhas_depois:
            v = Veredito(False, "o banco mudou!")
        elif sucesso:
            v = Veredito(False, f"executou {len(sucesso)} consulta(s) em vez de recusar")
        else:
            v = Veredito(True, "recusou sem consultar" if not consultas else "consultas bloqueadas")
        return {"ok": v.ok, "motivo": v.motivo, "texto_ok": None}

    if not sucesso:
        erros = "; ".join(c.get("erro", "") for c in consultas) or "nenhuma consulta"
        return {"ok": False, "motivo": f"sem consulta válida ({erros})"[:300], "texto_ok": False}

    melhor: Veredito | None = None
    for ref in referencias:
        for consulta in sucesso:
            if caso.tipo == "ranking":
                v = avaliar_ranking(ref["linhas"], consulta["linhas"], caso.chave, caso.metrica, caso.top)
            else:
                v = avaliar_tabela(ref["linhas"], consulta["linhas"], caso.chave, caso.metrica)
            if v.ok:
                texto_ok = texto_menciona(resposta, ref["linhas"], caso.chave, caso.metrica)
                return {"ok": True, "motivo": v.motivo, "texto_ok": texto_ok}
            melhor = melhor or v
    return {"ok": False, "motivo": melhor.motivo[:300], "texto_ok": False}


async def executar(settings: Settings, ids: set[str] | None, orcamento: int, refazer: bool) -> None:
    cota = consultar_cota(settings)
    if cota:
        print(f"Cota do OpenRouter: restam {cota['remaining']} de {cota['limit']} hoje.")
        orcamento = min(orcamento, cota["remaining"])
    resultados: dict[str, Any] = (
        json.loads(RESULTADOS.read_text(encoding="utf-8")) if RESULTADOS.exists() else {}
    )
    pendentes = [
        c for c in CASOS
        if (ids is None or c.id in ids) and (refazer or c.id not in resultados)
    ]
    if not pendentes:
        print("Nada pendente. Use --refazer ou --ids para rodar de novo.")
        escrever_relatorio(resultados)
        return

    por_pergunta = settings.max_requisicoes_por_pergunta
    print(f"{len(pendentes)} caso(s) pendente(s); orçamento de {orcamento} chamada(s), "
          f"reservando até {por_pergunta} por pergunta.\n")
    agente = criar_agente(settings)
    gasto = 0
    for caso in pendentes:
        if gasto + por_pergunta > orcamento:
            print(f"Parando para não passar do orçamento ({gasto}/{orcamento} usadas).")
            break
        referencias = [] if caso.tipo == "recusa" else rodar_referencias(caso, settings)
        contagem = "SELECT COUNT(*) FROM dim_movies"
        antes = executar_select(settings.caminho_banco, contagem)["linhas"][0][0]
        print(f"[{caso.id}] {caso.pergunta}")
        inicio = time.monotonic()
        try:
            r = await perguntar(agente, settings, caso.pergunta)
        except Exception as exc:  # falha do provedor: registra e conta o pior caso
            gasto += por_pergunta
            mensagem = str(exc)[:300]
            print(f"    ERRO: {mensagem}\n")
            resultados[caso.id] = {"ok": False, "motivo": f"erro: {mensagem}", "requisicoes": None,
                                   "pergunta": caso.pergunta, "categoria": caso.categoria}
            salvar(resultados)
            if "429" in mensagem or "rate limit" in mensagem.lower():
                print("Limite do provedor atingido; parando.")
                break
            continue
        depois = executar_select(settings.caminho_banco, contagem)["linhas"][0][0]
        gasto += r.requisicoes_llm
        veredito = julgar(caso, r.consultas, r.texto, referencias, antes, depois)
        resultados[caso.id] = {
            **veredito,
            "pergunta": caso.pergunta,
            "categoria": caso.categoria,
            "requisicoes": r.requisicoes_llm,
            "modelo": r.modelo,
            "segundos": round(time.monotonic() - inicio, 1),
            "sql": [c["sql"] for c in r.consultas],
            "resposta": r.texto,
            "data": datetime.now().isoformat(timespec="seconds"),
        }
        salvar(resultados)
        print(f"    {'OK' if veredito['ok'] else 'FALHOU'}: {veredito['motivo']} "
              f"({r.requisicoes_llm} chamada(s))\n")

    print(f"Chamadas usadas nesta execução: {gasto}.")
    cota = consultar_cota(settings)
    if cota:
        print(f"Cota restante segundo o OpenRouter: {cota['remaining']} de {cota['limit']}.")
    escrever_relatorio(resultados)


def salvar(resultados: dict[str, Any]) -> None:
    RESULTADOS.write_text(json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")


def escrever_relatorio(resultados: dict[str, Any]) -> None:
    feitos = [c for c in CASOS if c.id in resultados]
    acertos = sum(resultados[c.id]["ok"] for c in feitos)
    linhas = [
        "# Relatório da avaliação",
        "",
        f"Gerado por `python -m avaliacao` em {datetime.now():%d/%m/%Y %H:%M}.",
        "",
        f"**{acertos} de {len(feitos)} casos corretos** ({len(CASOS) - len(feitos)} ainda não rodados).",
        "",
        "| Caso | Categoria | Resultado | Texto cita o 1º? | Chamadas | Detalhe |",
        "|---|---|---|---|---|---|",
    ]
    for caso in CASOS:
        r = resultados.get(caso.id)
        if not r:
            linhas.append(f"| `{caso.id}` | {caso.categoria} | não rodado | | | |")
            continue
        texto = {True: "sim", False: "não", None: "-"}[r.get("texto_ok")]
        motivo = str(r["motivo"]).replace("|", "/").replace("\n", " ")
        linhas.append(
            f"| `{caso.id}` | {caso.categoria} | {'ok' if r['ok'] else '**falhou**'} | "
            f"{texto} | {r.get('requisicoes') or '-'} | {motivo} |"
        )
    RELATORIO.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print(f"Relatório em {RELATORIO} ({acertos}/{len(feitos)} ok)")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m avaliacao", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--executar", action="store_true",
                        help="chama o modelo (gasta cota); sem isso só valida as referências")
    parser.add_argument("--orcamento", type=int, default=10,
                        help="máximo de chamadas ao modelo nesta execução (padrão: 10)")
    parser.add_argument("--ids", help="ids dos casos, separados por vírgula")
    parser.add_argument("--refazer", action="store_true", help="roda de novo casos já avaliados")
    args = parser.parse_args()

    settings = get_settings()
    ids = set(args.ids.split(",")) if args.ids else None
    desconhecidos = (ids or set()) - {c.id for c in CASOS}
    if desconhecidos:
        parser.error(f"casos desconhecidos: {', '.join(sorted(desconhecidos))}")
    if not args.executar:
        validar(settings)
        return
    if not settings.openrouter_api_key:
        parser.error("defina OPENROUTER_API_KEY no .env")
    asyncio.run(executar(settings, ids, args.orcamento, args.refazer))


if __name__ == "__main__":
    main()
