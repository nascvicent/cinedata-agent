"""Testa o fluxo completo do agente e da API com um modelo falso (zero requisições reais)."""

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app import main
from app.agent import criar_agente
from app.config import Settings


def modelo_falso(mensagens: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if len(mensagens) == 1:
        sql = (
            "SELECT m.titulo, f.receita_brl FROM fact_movies_performance f "
            "JOIN dim_movies m USING (sk_movie_id) ORDER BY f.receita_brl DESC LIMIT 1"
        )
        return ModelResponse(parts=[ToolCallPart("executar_sql", {"sql": sql})])
    retorno = mensagens[-1].parts[0].content
    return ModelResponse(parts=[TextPart(f"Resultado: {retorno}")])


def test_api_pergunta_e_cache(banco, monkeypatch):
    settings = Settings(openrouter_api_key="teste", caminho_banco=banco, _env_file=None)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main, "criar_agente", lambda s: criar_agente(s, FunctionModel(modelo_falso))
    )

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
