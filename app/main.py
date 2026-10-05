"""API FastAPI do agente CineData.

Rotas:
- GET  /          : interface web de perguntas e respostas
- POST /perguntar : pergunta em linguagem natural -> resposta + SQL executado
- POST /perguntar/stream : mesma coisa, em etapas (Server-Sent Events)
- GET  /schema    : schema que o agente enxerga
- GET  /cota      : uso da cota diária de modelos gratuitos no OpenRouter
- GET  /health    : verificação simples
"""

import json
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic_ai.exceptions import AgentRunError, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage

from app.agent import (
    OPENROUTER_BASE_URL,
    Resposta,
    criar_agente,
    perguntar,
    perguntar_em_etapas,
    turno_para_historico,
)
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
    app.state.cache = {}  # pergunta normalizada -> (PerguntaResponse, turno)
    app.state.conversas = OrderedDict()  # conversa_id -> turnos recentes
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


def _erro_http(exc: Exception) -> HTTPException:
    """Traduz as falhas do agente para o erro HTTP devolvido ao cliente."""
    if isinstance(exc, UsageLimitExceeded):
        return HTTPException(
            422, "O agente não chegou a uma resposta dentro do limite de chamadas. Reformule."
        )
    # ExceptionGroup: todos os modelos da cadeia de fallback falharam.
    erros = exc.exceptions if isinstance(exc, ExceptionGroup) else [exc]
    detalhe = "; ".join(str(e) for e in erros)[:600]
    return HTTPException(
        503, f"Nenhum modelo respondeu (cota esgotada ou provedores lotados). {detalhe}"
    )


MAX_TURNOS = 5  # turnos anteriores enviados ao modelo como histórico
MAX_CONVERSAS = 500  # conversas guardadas em memória (as mais antigas saem primeiro)


@dataclass
class Conversa:
    """Estado de uma pergunta dentro de uma conversa (histórico + cache)."""

    id: str
    turnos: list[list[ModelMessage]]
    chave_cache: str | None  # só a 1ª pergunta da conversa usa o cache

    @property
    def historico(self) -> list[ModelMessage]:
        return [m for turno in self.turnos for m in turno]


def _abrir_conversa(estado, body: PerguntaRequest) -> Conversa:
    conversa_id = body.conversa_id or uuid.uuid4().hex
    turnos = estado.conversas.get(conversa_id, [])
    return Conversa(conversa_id, turnos, None if turnos else _normalizar(body.pergunta))


def _registrar_turno(estado, conversa: Conversa, turno: list[ModelMessage]) -> None:
    estado.conversas[conversa.id] = (conversa.turnos + [turno])[-MAX_TURNOS:]
    estado.conversas.move_to_end(conversa.id)
    while len(estado.conversas) > MAX_CONVERSAS:
        estado.conversas.popitem(last=False)


def _do_cache(estado, conversa: Conversa) -> PerguntaResponse | None:
    if conversa.chave_cache not in estado.cache:
        return None
    resposta, turno = estado.cache[conversa.chave_cache]
    _registrar_turno(estado, conversa, turno)
    return resposta.model_copy(update={"cache": True, "conversa_id": conversa.id})


def _concluir(estado, conversa: Conversa, pergunta: str, resultado: Resposta) -> PerguntaResponse:
    resposta = PerguntaResponse(
        resposta=resultado.texto,
        consultas=resultado.consultas,
        modelo=resultado.modelo,
        requisicoes_llm=resultado.requisicoes_llm,
        conversa_id=conversa.id,
    )
    turno = turno_para_historico(pergunta, resultado)
    _registrar_turno(estado, conversa, turno)
    # Só guarda respostas apoiadas em dados: recusas e respostas sem consulta válida
    # (ex.: o modelo errou) não devem se repetir para sempre.
    if conversa.chave_cache and any(not c.get("erro") for c in resultado.consultas):
        estado.cache[conversa.chave_cache] = (resposta, turno)
    return resposta


@app.post("/perguntar", response_model=PerguntaResponse)
async def rota_perguntar(body: PerguntaRequest, request: Request) -> PerguntaResponse:
    estado = request.app.state
    conversa = _abrir_conversa(estado, body)
    if (do_cache := _do_cache(estado, conversa)) is not None:
        return do_cache

    try:
        resultado = await perguntar(
            estado.agente, estado.settings, body.pergunta, conversa.historico
        )
    except (AgentRunError, ExceptionGroup) as exc:
        raise _erro_http(exc) from exc
    return _concluir(estado, conversa, body.pergunta, resultado)


def _sse(evento: dict) -> str:
    return f"data: {json.dumps(evento, ensure_ascii=False, default=str)}\n\n"


@app.post("/perguntar/stream")
async def rota_perguntar_stream(body: PerguntaRequest, request: Request) -> StreamingResponse:
    """Responde em etapas via Server-Sent Events, para a interface mostrar o progresso.

    Cada evento é um JSON com `tipo`: `consulta`, `resultado`, `texto`, `fim` ou `erro`.
    O evento `fim` traz o mesmo corpo de `POST /perguntar`.
    """
    estado = request.app.state
    conversa = _abrir_conversa(estado, body)

    async def eventos() -> AsyncIterator[str]:
        if (do_cache := _do_cache(estado, conversa)) is not None:
            yield _sse({"tipo": "fim", **do_cache.model_dump()})
            return
        try:
            async for evento in perguntar_em_etapas(
                estado.agente, estado.settings, body.pergunta, conversa.historico
            ):
                if evento["tipo"] == "fim":
                    resposta = _concluir(estado, conversa, body.pergunta, evento["resposta"])
                    yield _sse({"tipo": "fim", **resposta.model_dump()})
                else:
                    yield _sse(evento)
        except (AgentRunError, ExceptionGroup) as exc:
            erro = _erro_http(exc)
            yield _sse({"tipo": "erro", "status": erro.status_code, "detail": erro.detail})

    return StreamingResponse(
        eventos(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
