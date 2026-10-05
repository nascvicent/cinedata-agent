"""Testa o fluxo completo do agente e da API com um modelo falso (zero requisições reais)."""

import json
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from app import main
from app.agent import criar_agente
from app.config import Settings


SQL_FALSO = (
    "SELECT m.titulo, f.receita_brl FROM fact_movies_performance f "
    "JOIN dim_movies m USING (sk_movie_id) ORDER BY f.receita_brl DESC LIMIT 1"
)


# Mensagens recebidas pelo modelo falso em cada chamada, para conferir o histórico.
CHAMADAS: list[list[ModelMessage]] = []


def _acabou_de_consultar(mensagens: list[ModelMessage]) -> bool:
    return any(isinstance(p, ToolReturnPart) for p in mensagens[-1].parts)


def modelo_falso(mensagens: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    CHAMADAS.append(list(mensagens))
    if not _acabou_de_consultar(mensagens):
        return ModelResponse(parts=[ToolCallPart("executar_sql", {"sql": SQL_FALSO})])
    retorno = mensagens[-1].parts[0].content
    return ModelResponse(parts=[TextPart(f"Resultado: {retorno}")])


async def modelo_falso_stream(
    mensagens: list[ModelMessage], info: AgentInfo
) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
    CHAMADAS.append(list(mensagens))
    if not _acabou_de_consultar(mensagens):
        yield {0: DeltaToolCall("executar_sql", json.dumps({"sql": SQL_FALSO}))}
        return
    for trecho in ["O maior ", "é o Filme B."]:
        yield trecho


def configurar(banco, monkeypatch) -> None:
    CHAMADAS.clear()
    settings = Settings(openrouter_api_key="teste", caminho_banco=banco, _env_file=None)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    modelo = FunctionModel(modelo_falso, stream_function=modelo_falso_stream)
    monkeypatch.setattr(main, "criar_agente", lambda s: criar_agente(s, modelo))


def ler_eventos(texto: str) -> list[dict]:
    return [json.loads(linha[6:]) for linha in texto.splitlines() if linha.startswith("data: ")]


def test_api_pergunta_e_cache(banco, monkeypatch):
    configurar(banco, monkeypatch)

    with TestClient(main.app) as cliente:
        r1 = cliente.post("/perguntar", json={"pergunta": "Qual filme teve maior receita?"})
        assert r1.status_code == 200
        corpo = r1.json()
        assert "Filme B" in corpo["resposta"]
        assert corpo["consultas"][0]["linhas"] == [["Filme B", 300.0]]
        assert corpo["requisicoes_llm"] == 2 and corpo["cache"] is False

        r2 = cliente.post("/perguntar", json={"pergunta": "qual filme teve maior receita"})
        assert r2.json()["cache"] is True

        assert "dim_movies" in cliente.get("/schema").text

        pagina = cliente.get("/")
        assert pagina.status_code == 200 and "CineData" in pagina.text


def test_api_stream_em_etapas(banco, monkeypatch):
    configurar(banco, monkeypatch)

    with TestClient(main.app) as cliente:
        r = cliente.post("/perguntar/stream", json={"pergunta": "Qual filme teve maior receita?"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        eventos = ler_eventos(r.text)
        tipos = [e["tipo"] for e in eventos]
        assert tipos == ["consulta", "resultado", "texto", "texto", "fim"]
        assert eventos[0]["sql"] == SQL_FALSO
        assert eventos[1]["linhas"] == [["Filme B", 300.0]]
        fim = eventos[-1]
        assert fim["resposta"] == "O maior é o Filme B."
        assert fim["consultas"][0]["linhas"] == [["Filme B", 300.0]]
        assert fim["requisicoes_llm"] == 2 and fim["cache"] is False

        # A pergunta respondida em stream também alimenta o cache das duas rotas.
        repetida = ler_eventos(
            cliente.post("/perguntar/stream", json={"pergunta": "qual filme teve maior receita"}).text
        )
        assert [e["tipo"] for e in repetida] == ["fim"] and repetida[0]["cache"] is True
        assert cliente.post("/perguntar", json={"pergunta": "Qual filme teve maior receita"}).json()["cache"]


def _textos(mensagens: list[ModelMessage]) -> str:
    return " ".join(str(getattr(p, "content", "")) for m in mensagens for p in m.parts)


def test_historico_de_conversa(banco, monkeypatch):
    configurar(banco, monkeypatch)

    with TestClient(main.app) as cliente:
        r1 = cliente.post("/perguntar", json={"pergunta": "Qual filme teve maior receita?"}).json()
        conversa = r1["conversa_id"]
        assert conversa

        CHAMADAS.clear()
        r2 = cliente.post(
            "/perguntar", json={"pergunta": "E o segundo?", "conversa_id": conversa}
        ).json()
        assert r2["conversa_id"] == conversa
        enviado = _textos(CHAMADAS[0])
        assert "Qual filme teve maior receita?" in enviado
        assert "[SQL usado: " + SQL_FALSO in enviado
        # Só pergunta + resposta final viram histórico: nada de chamadas de ferramenta antigas.
        assert len(CHAMADAS[0]) == 3

        # No meio de uma conversa o cache não vale: a resposta depende do contexto.
        r3 = cliente.post(
            "/perguntar",
            json={"pergunta": "Qual filme teve maior receita?", "conversa_id": conversa},
        ).json()
        assert r3["cache"] is False

        # Conversa nova com pergunta repetida usa o cache e já guarda o turno no histórico.
        r4 = cliente.post("/perguntar", json={"pergunta": "qual filme teve maior receita"}).json()
        assert r4["cache"] is True and r4["conversa_id"] != conversa
        CHAMADAS.clear()
        eventos = ler_eventos(
            cliente.post(
                "/perguntar/stream",
                json={"pergunta": "E em dólar?", "conversa_id": r4["conversa_id"]},
            ).text
        )
        assert eventos[-1]["conversa_id"] == r4["conversa_id"]
        assert "Qual filme teve maior receita?" in _textos(CHAMADAS[0])


def test_historico_guarda_so_os_ultimos_turnos(banco, monkeypatch):
    configurar(banco, monkeypatch)

    with TestClient(main.app) as cliente:
        conversa = None
        for i in range(main.MAX_TURNOS + 2):
            corpo = {"pergunta": f"Pergunta numero {i}", "conversa_id": conversa}
            conversa = cliente.post("/perguntar", json=corpo).json()["conversa_id"]
        CHAMADAS.clear()
        cliente.post("/perguntar", json={"pergunta": "Mais uma", "conversa_id": conversa})
        enviado = _textos(CHAMADAS[0])
        assert "Pergunta numero 0" not in enviado and "Pergunta numero 1" not in enviado
        assert f"Pergunta numero {main.MAX_TURNOS + 1}" in enviado


def test_sql_no_texto_volta_para_o_modelo_e_nao_entra_no_cache(banco, monkeypatch):
    """Modelo que 'responde' escrevendo SQL é mandado executar; recusa não vai pro cache."""
    respostas = iter(
        [
            ModelResponse(parts=[TextPart("SELECT titulo FROM dim_movies")]),
            ModelResponse(parts=[ToolCallPart("executar_sql", {"sql": SQL_FALSO})]),
            ModelResponse(parts=[TextPart("O maior é o Filme B.")]),
            ModelResponse(parts=[TextPart("Só respondo sobre o catálogo.")]),
            ModelResponse(parts=[TextPart("Só respondo sobre o catálogo.")]),
        ]
    )
    settings = Settings(openrouter_api_key="teste", caminho_banco=banco, _env_file=None)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    modelo = FunctionModel(lambda mensagens, info: next(respostas))
    monkeypatch.setattr(main, "criar_agente", lambda s: criar_agente(s, modelo))

    with TestClient(main.app) as cliente:
        r = cliente.post("/perguntar", json={"pergunta": "Qual filme teve maior receita?"}).json()
        assert r["resposta"] == "O maior é o Filme B."
        assert r["consultas"][0]["linhas"] == [["Filme B", 300.0]]
        assert r["requisicoes_llm"] == 3

        for _ in range(2):
            r = cliente.post("/perguntar", json={"pergunta": "Qual a capital da França?"}).json()
            assert r["cache"] is False
