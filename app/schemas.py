"""Modelos de entrada e saída da API."""

from typing import Any

from pydantic import BaseModel, Field


class PerguntaRequest(BaseModel):
    pergunta: str = Field(
        min_length=3,
        max_length=500,
        examples=["Quais são os 10 filmes com maior receita em R$?"],
    )
    conversa_id: str | None = Field(
        default=None,
        max_length=64,
        description="Id devolvido na resposta anterior, para perguntas de continuação. "
        "Omita para começar uma conversa nova.",
    )


class ConsultaExecutada(BaseModel):
    sql: str
    colunas: list[str] = []
    linhas: list[list[Any]] = []
    truncado: bool = False
    erro: str | None = None


class PerguntaResponse(BaseModel):
    resposta: str
    consultas: list[ConsultaExecutada]
    modelo: str | None = None
    requisicoes_llm: int = 0
    cache: bool = False
    conversa_id: str | None = None
