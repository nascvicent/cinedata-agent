"""Agente Text-to-SQL construído com PydanticAI sobre o OpenRouter."""

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from pydantic_ai import Agent, AgentRunResultEvent, ModelRetry, RunContext
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.usage import UsageLimits

from app.config import Settings
from app.database import ConsultaInvalida, descrever_schema, executar_select
from app.prompts import montar_system_prompt

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# SQL escrito no texto da resposta (ex.: "SELECT ... FROM dim_movies").
SQL_NO_TEXTO = re.compile(
    r"\bselect\b.+?\bfrom\b|\bfrom\s+(dim|fact|bridge)_\w+", re.IGNORECASE | re.DOTALL
)


@dataclass
class Contexto:
    """Dependências de uma execução; também registra as consultas feitas."""

    settings: Settings
    consultas: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Resposta:
    texto: str
    consultas: list[dict[str, Any]]
    modelo: str | None
    requisicoes_llm: int


def executar_sql(ctx: RunContext[Contexto], sql: str) -> str:
    """Executa uma consulta SELECT no banco SQLite da CineData e devolve o resultado em JSON.

    Args:
        sql: Uma única instrução SELECT (ou WITH ... SELECT) em dialeto SQLite.
    """
    cfg = ctx.deps.settings
    registro: dict[str, Any] = {"sql": sql, "id": ctx.tool_call_id}
    ctx.deps.consultas.append(registro)
    try:
        resultado = executar_select(
            cfg.caminho_banco, sql, cfg.max_linhas, cfg.timeout_sql_segundos
        )
    except ConsultaInvalida as exc:
        registro["erro"] = str(exc)
        return f"ERRO: {exc}"
    registro.update(resultado)
    return json.dumps(resultado, ensure_ascii=False, default=str)


def exigir_consulta(ctx: RunContext[Contexto], resposta: str) -> str:
    """Barra respostas que trazem SQL no texto sem ter executado nada.

    Alguns modelos gratuitos às vezes "respondem" escrevendo a consulta em vez de chamar
    a ferramenta; o ModelRetry devolve a instrução ao modelo para ele tentar de novo.
    """
    if not ctx.deps.consultas and SQL_NO_TEXTO.search(resposta):
        raise ModelRetry(
            "Você escreveu SQL na resposta sem executá-lo. Chame a ferramenta executar_sql "
            "com a consulta e responda em português com base no resultado."
        )
    return resposta


def criar_modelo(settings: Settings) -> Model:
    """Monta a cadeia de fallback entre os modelos configurados.

    `max_retries=0` é intencional: no OpenRouter, requisições que falham também contam
    na cota diária, então em vez de repetir no mesmo modelo passamos para o próximo.
    """
    cliente = AsyncOpenAI(
        base_url=OPENROUTER_BASE_URL, api_key=settings.openrouter_api_key, max_retries=0
    )
    provider = OpenRouterProvider(openai_client=cliente)
    modelos = [OpenAIChatModel(nome, provider=provider) for nome in settings.lista_modelos]
    if not modelos:
        raise ValueError("Configure ao menos um modelo em MODELOS.")
    return modelos[0] if len(modelos) == 1 else FallbackModel(*modelos)


def criar_agente(settings: Settings, modelo: Model | None = None) -> Agent[Contexto, str]:
    schema = descrever_schema(settings.caminho_banco)
    agente = Agent(
        modelo or criar_modelo(settings),
        deps_type=Contexto,
        instructions=montar_system_prompt(schema, settings.max_linhas),
        tools=[executar_sql],
        model_settings={"temperature": 0},
    )
    agente.output_validator(exigir_consulta)
    return agente


def turno_para_historico(pergunta: str, resposta: Resposta) -> list[ModelMessage]:
    """Resume uma pergunta já respondida para servir de histórico à próxima.

    Guarda só a pergunta, o texto final e o SQL que deu certo, sem as chamadas de
    ferramenta nem os dados brutos: o modelo tem contexto para continuações ("e em
    2023?") sem reenviar dezenas de linhas a cada chamada.
    """
    texto = resposta.texto
    sqls = [c["sql"] for c in resposta.consultas if not c.get("erro")]
    if sqls:
        texto += f"\n\n[SQL usado: {sqls[-1]}]"
    return [
        ModelRequest(parts=[UserPromptPart(pergunta)]),
        ModelResponse(parts=[TextPart(texto)]),
    ]


async def perguntar(
    agente: Agent[Contexto, str],
    settings: Settings,
    pergunta: str,
    historico: list[ModelMessage] | None = None,
) -> Resposta:
    contexto = Contexto(settings=settings)
    resultado = await agente.run(
        pergunta,
        message_history=historico or None,
        deps=contexto,
        usage_limits=UsageLimits(request_limit=settings.max_requisicoes_por_pergunta),
    )
    return Resposta(
        texto=resultado.output,
        consultas=contexto.consultas,
        modelo=resultado.response.model_name,
        requisicoes_llm=resultado.usage.requests,
    )


async def perguntar_em_etapas(
    agente: Agent[Contexto, str],
    settings: Settings,
    pergunta: str,
    historico: list[ModelMessage] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Mesmo fluxo de `perguntar`, mas emite cada etapa assim que ela acontece.

    Eventos: `consulta` (SQL que vai rodar), `resultado` (dados ou erro da consulta),
    `texto` (trecho da resposta) e, por último, `fim` (com a `Resposta` completa).
    """
    contexto = Contexto(settings=settings)
    async with agente.run_stream_events(
        pergunta,
        message_history=historico or None,
        deps=contexto,
        usage_limits=UsageLimits(request_limit=settings.max_requisicoes_por_pergunta),
    ) as eventos:
        async for evento in eventos:
            if isinstance(evento, PartStartEvent) and isinstance(evento.part, TextPart):
                if evento.part.content:
                    yield {"tipo": "texto", "trecho": evento.part.content}
            elif isinstance(evento, PartDeltaEvent) and isinstance(evento.delta, TextPartDelta):
                yield {"tipo": "texto", "trecho": evento.delta.content_delta}
            elif isinstance(evento, FunctionToolCallEvent):
                sql = evento.part.args_as_dict().get("sql", "")
                yield {"tipo": "consulta", "sql": sql}
            elif isinstance(evento, FunctionToolResultEvent):
                registro = next(
                    (c for c in contexto.consultas if c.get("id") == evento.tool_call_id), None
                )
                if registro:
                    dados = {k: v for k, v in registro.items() if k != "id"}
                    yield {"tipo": "resultado", **dados}
            elif isinstance(evento, AgentRunResultEvent):
                resultado = evento.result
                yield {
                    "tipo": "fim",
                    "resposta": Resposta(
                        texto=resultado.output,
                        consultas=contexto.consultas,
                        modelo=resultado.response.model_name,
                        requisicoes_llm=resultado.usage.requests,
                    ),
                }
