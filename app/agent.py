"""Agente Text-to-SQL construído com PydanticAI sobre o OpenRouter."""

import json
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.usage import UsageLimits

from app.config import Settings
from app.database import ConsultaInvalida, descrever_schema, executar_select
from app.prompts import montar_system_prompt

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


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
    registro: dict[str, Any] = {"sql": sql}
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
    return Agent(
        modelo or criar_modelo(settings),
        deps_type=Contexto,
        instructions=montar_system_prompt(schema, settings.max_linhas),
        tools=[executar_sql],
        model_settings={"temperature": 0},
    )


async def perguntar(agente: Agent[Contexto, str], settings: Settings, pergunta: str) -> Resposta:
    contexto = Contexto(settings=settings)
    resultado = await agente.run(
        pergunta,
        deps=contexto,
        usage_limits=UsageLimits(request_limit=settings.max_requisicoes_por_pergunta),
    )
    return Resposta(
        texto=resultado.output,
        consultas=contexto.consultas,
        modelo=resultado.response.model_name,
        requisicoes_llm=resultado.usage.requests,
    )
