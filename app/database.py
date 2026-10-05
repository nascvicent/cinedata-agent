"""Acesso somente leitura à camada Gold (SQLite) e introspecção do schema.

Guardrails aplicados em toda consulta vinda do agente:
1. conexão aberta em modo read-only (`mode=ro`);
2. authorizer do SQLite que só permite SELECT, leitura de colunas e funções;
3. tabelas internas bloqueadas;
4. limite de linhas retornadas e de tempo de execução.
"""

import sqlite3
import time
from pathlib import Path
from typing import Any

TABELAS_OCULTAS = {"alembic_version"}
# Colunas de texto livre: não vale a pena amostrar valores distintos.
COLUNAS_TEXTO_LIVRE = {"sinopse", "text", "titulo", "name", "url_poster", "url_backdrop"}
MAX_VALORES_DISTINTOS = 12

_ACOES_PERMITIDAS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


class ConsultaInvalida(Exception):
    """Consulta rejeitada pelos guardrails ou com erro de SQL."""


def _conectar(caminho: str) -> sqlite3.Connection:
    if not Path(caminho).is_file():
        raise FileNotFoundError(
            f"Banco '{caminho}' não encontrado. Coloque o cinerocket.db na raiz do projeto."
        )
    return sqlite3.connect(f"file:{Path(caminho).as_posix()}?mode=ro", uri=True)


def _authorizer(acao: int, arg1: str | None, *_: Any) -> int:
    if acao not in _ACOES_PERMITIDAS:
        return sqlite3.SQLITE_DENY
    if acao == sqlite3.SQLITE_READ and arg1 and (
        arg1 in TABELAS_OCULTAS or arg1.startswith("sqlite_")
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def executar_select(
    caminho: str, sql: str, max_linhas: int = 50, timeout: float = 5.0
) -> dict[str, Any]:
    """Executa uma única consulta de leitura e devolve colunas, linhas e se houve corte."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise ConsultaInvalida("Consulta vazia.")
    if not sql.lower().startswith(("select", "with")):
        raise ConsultaInvalida("Apenas consultas SELECT (ou WITH ... SELECT) são permitidas.")

    conn = _conectar(caminho)
    try:
        conn.set_authorizer(_authorizer)
        limite = time.monotonic() + timeout
        conn.set_progress_handler(lambda: int(time.monotonic() > limite), 10_000)

        cursor = conn.execute(sql)  # execute() recusa mais de uma instrução
        colunas = [d[0] for d in cursor.description or []]
        linhas = cursor.fetchmany(max_linhas + 1)
    except (sqlite3.Warning, sqlite3.ProgrammingError) as exc:
        raise ConsultaInvalida("Envie apenas uma instrução SQL por vez.") from exc
    except sqlite3.DatabaseError as exc:
        mensagem = str(exc)
        if "interrupted" in mensagem:
            raise ConsultaInvalida(
                f"Consulta excedeu {timeout:.0f}s. Simplifique ou filtre mais."
            ) from exc
        if "not authorized" in mensagem or "prohibited" in mensagem:
            raise ConsultaInvalida("Operação não permitida: o acesso é somente leitura.") from exc
        raise ConsultaInvalida(f"Erro de SQL: {mensagem}") from exc
    finally:
        conn.close()

    truncado = len(linhas) > max_linhas
    return {
        "colunas": colunas,
        "linhas": [list(linha) for linha in linhas[:max_linhas]],
        "truncado": truncado,
    }


def descrever_schema(caminho: str) -> str:
    """Gera a descrição textual do schema direto do banco (sem gastar chamadas ao LLM).

    Para colunas de texto com poucos valores distintos (ex.: tipo_pessoa), lista os
    valores reais, para o modelo não precisar adivinhar como filtrar.
    """
    conn = _conectar(caminho)
    try:
        tabelas = [
            nome
            for (nome,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            if nome not in TABELAS_OCULTAS
        ]
        blocos = []
        for tabela in tabelas:
            linhas = [f"{tabela}"]
            for _, coluna, tipo, _, _, pk in conn.execute(f'PRAGMA table_info("{tabela}")'):
                descricao = f"  - {coluna} ({tipo or 'TEXT'})"
                if pk:
                    descricao += " [PK]"
                if _e_texto(tipo) and coluna not in COLUNAS_TEXTO_LIVRE and "_id" not in coluna:
                    valores = [
                        v
                        for (v,) in conn.execute(
                            f'SELECT DISTINCT "{coluna}" FROM "{tabela}" '
                            f'WHERE "{coluna}" IS NOT NULL LIMIT {MAX_VALORES_DISTINTOS + 1}'
                        )
                    ]
                    if 0 < len(valores) <= MAX_VALORES_DISTINTOS:
                        descricao += " valores: " + ", ".join(repr(v) for v in valores)
                linhas.append(descricao)
            blocos.append("\n".join(linhas))
        return "\n\n".join(blocos)
    finally:
        conn.close()


def _e_texto(tipo: str | None) -> bool:
    tipo = (tipo or "").upper()
    return tipo == "" or "CHAR" in tipo or "TEXT" in tipo
