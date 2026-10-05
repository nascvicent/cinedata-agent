"""Testa o fluxo completo do agente e da API com um modelo falso (zero requisições reais)."""

from fastapi.testclient import TestClient
import json
from collections.abc import AsyncIterator

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from app import main
from app.agent import criar_agente
from app.config import Settings


SQL_FALSO = (
    "SELECT m.titulo, f.receita_brl FROM fact_movies_performance f "
    "JOIN dim_movies m USING (sk_movie_id) ORDER BY f.receita_brl DESC LIMIT 1"
)


def modelo_falso(mensagens: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if len(mensagens) == 1:
        return ModelResponse(parts=[ToolCallPart("executar_sql", {"sql": SQL_FALSO})])
    retorno = mensagens[-1].parts[0].content
    return ModelResponse(parts=[TextPart(f"Resultado: {retorno}")])


async def modelo_falso_stream(
    mensagens: list[ModelMessage], info: AgentInfo
) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
    if len(mensagens) == 1:
        yield {0: DeltaToolCall("executar_sql", json.dumps({"sql": SQL_FALSO}))}
        return
    for trecho in ["O maior ", "é o Filme B."]:
        yield trecho


def configurar(banco, monkeypatch) -> None:
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
