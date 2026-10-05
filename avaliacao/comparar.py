"""Compara o resultado do agente com o da consulta de referência.

A comparação olha para os valores, não para o SQL: o agente pode nomear colunas,
ordená-las ou arredondar como quiser. Em rankings, empates na última posição pedida
são aceitos (qualquer um dos filmes empatados serve).
"""

from dataclasses import dataclass
from typing import Any

TOLERANCIA_RELATIVA = 0.01
TOLERANCIA_ABSOLUTA = 0.05


@dataclass
class Veredito:
    ok: bool
    motivo: str


def _texto(valor: Any) -> str:
    return " ".join(str(valor).casefold().split())


def _numero(valor: Any) -> float | None:
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    try:
        return float(str(valor).replace(",", "."))
    except ValueError:
        return None


def numeros_proximos(a: float, b: float) -> bool:
    """Igualdade com tolerância; também aceita o valor apresentado em percentual."""
    for candidato in (b, b * 100):
        if abs(a - candidato) <= max(TOLERANCIA_ABSOLUTA, TOLERANCIA_RELATIVA * abs(candidato)):
            return True
    return False


def valor_presente(valor: Any, linha: list[Any]) -> bool:
    numero = _numero(valor)
    if numero is not None and not isinstance(valor, str):
        return any(
            (n := _numero(c)) is not None and abs(n - numero) < 1e-9 for c in linha
        )
    alvo = _texto(valor)
    return any(_texto(c) == alvo for c in linha)


def linha_contem(chave: tuple[Any, ...], linha: list[Any]) -> bool:
    return all(valor_presente(v, linha) for v in chave)


def avaliar_ranking(
    referencia: list[list[Any]],
    agente: list[list[Any]],
    chave: tuple[int, ...],
    metrica: int,
    top: int,
) -> Veredito:
    """As `top` primeiras linhas do agente precisam estar entre as `top` da referência
    (contando empates com a última posição)."""
    if not referencia:
        return Veredito(not agente, "referência vazia")
    n = min(top, len(referencia))
    limiar = referencia[n - 1][metrica]
    aceitas = [
        tuple(linha[i] for i in chave)
        for linha in referencia
        if linha[metrica] is not None and linha[metrica] >= limiar - 1e-9
    ]
    if len(agente) < n:
        return Veredito(False, f"agente trouxe {len(agente)} linha(s), esperado {n}")
    for posicao, linha in enumerate(agente[:n], 1):
        if not any(linha_contem(c, linha) for c in aceitas):
            return Veredito(False, f"posição {posicao} fora do esperado: {linha}")
    return Veredito(True, f"top {n} confere")


def avaliar_tabela(
    referencia: list[list[Any]], agente: list[list[Any]], chave: tuple[int, ...], metrica: int
) -> Veredito:
    """Toda linha da referência precisa aparecer no agente, com o valor próximo."""
    for linha_ref in referencia:
        valores_chave = tuple(linha_ref[i] for i in chave)
        esperado = linha_ref[metrica]
        achou = False
        for linha in agente:
            if not linha_contem(valores_chave, linha):
                continue
            if esperado is None or any(
                (n := _numero(c)) is not None and numeros_proximos(n, float(esperado))
                for c in linha
            ):
                achou = True
                break
        if not achou:
            return Veredito(False, f"faltou ou divergiu: {list(valores_chave)} = {esperado}")
    return Veredito(True, f"{len(referencia)} linhas conferem")


def texto_menciona(
    resposta: str, referencia: list[list[Any]], chave: tuple[int, ...], metrica: int
) -> bool:
    """A resposta em texto cita o 1º colocado da referência (ou um empatado com ele).

    É só um sinal: o modelo pode, por exemplo, traduzir um título.
    """
    if not referencia:
        return True
    topo = referencia[0][metrica]
    candidatos = [linha[chave[0]] for linha in referencia if linha[metrica] == topo]
    texto = _texto(resposta.replace("*", ""))
    return any(not isinstance(c, str) or _texto(c) in texto for c in candidatos)
