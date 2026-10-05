"""API FastAPI do agente CineData.

Rotas:
- GET  /          : interface web de perguntas e respostas
- POST /perguntar : pergunta em linguagem natural -> resposta + SQL executado
- GET  /schema    : schema que o agente enxerga
- GET  /cota      : uso da cota diária de modelos gratuitos no OpenRouter
- GET  /health    : verificação simples
"""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic_ai.exceptions import AgentRunError, UsageLimitExceeded

from app.agent import OPENROUTER_BASE_URL, criar_agente, perguntar
from app.config import get_settings
from app.database import descrever_schema
from app.schemas import PerguntaRequest, PerguntaResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError("Defina OPENROUTER_API_KEY no arquivo .env (veja .env.example).")
    app.state.settings = settings
    app.state.agente = criar_agente(settings)
    app.state.cache = {}  # pergunta normalizada -> PerguntaResponse
    yield


app = FastAPI(
    title="CineData Agent",
    description="Agente Text-to-SQL sobre a camada Gold da CineData Analytics.",
    version="1.0.0",
    lifespan=lifespan,
)


PAGINA = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def pagina() -> FileResponse:
    return FileResponse(PAGINA)


def _normalizar(pergunta: str) -> str:
    return " ".join(pergunta.lower().split()).rstrip("?.! ")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/schema", response_class=PlainTextResponse)
def schema(request: Request) -> str:
    return descrever_schema(request.app.state.settings.caminho_banco)


@app.get("/cota")
async def cota(request: Request) -> dict:
    chave = request.app.state.settings.openrouter_api_key
    async with httpx.AsyncClient(timeout=10) as cliente:
        resp = await cliente.get(
            f"{OPENROUTER_BASE_URL}/key", headers={"Authorization": f"Bearer {chave}"}
        )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "Não foi possível consultar a cota no OpenRouter.")
    return resp.json()


@app.post("/perguntar", response_model=PerguntaResponse)
async def rota_perguntar(body: PerguntaRequest, request: Request) -> PerguntaResponse:
    estado = request.app.state
    chave_cache = _normalizar(body.pergunta)

    if chave_cache in estado.cache:
        return estado.cache[chave_cache].model_copy(update={"cache": True})

    try:
        resultado = await perguntar(estado.agente, estado.settings, body.pergunta)
    except UsageLimitExceeded as exc:
        raise HTTPException(
            422, "O agente não chegou a uma resposta dentro do limite de chamadas. Reformule."
        ) from exc
    except (AgentRunError, ExceptionGroup) as exc:
        # ExceptionGroup: todos os modelos da cadeia de fallback falharam.
        erros = exc.exceptions if isinstance(exc, ExceptionGroup) else [exc]
        detalhe = "; ".join(str(e) for e in erros)[:600]
        raise HTTPException(
            503, f"Nenhum modelo respondeu (cota esgotada ou provedores lotados). {detalhe}"
        ) from exc

    resposta = PerguntaResponse(
        resposta=resultado.texto,
        consultas=resultado.consultas,
        modelo=resultado.modelo,
        requisicoes_llm=resultado.requisicoes_llm,
    )
    estado.cache[chave_cache] = resposta
    return resposta
