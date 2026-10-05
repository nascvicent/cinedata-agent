import pytest

from app.database import ConsultaInvalida, descrever_schema, executar_select


def test_select_simples(banco):
    r = executar_select(banco, "SELECT titulo FROM dim_movies ORDER BY titulo;")
    assert r["colunas"] == ["titulo"]
    assert r["linhas"] == [["Filme A"], ["Filme B"]]
    assert r["truncado"] is False


def test_cte_e_join(banco):
    sql = """WITH t AS (SELECT sk_movie_id, receita_brl FROM fact_movies_performance)
             SELECT m.titulo FROM t JOIN dim_movies m USING (sk_movie_id)
             ORDER BY receita_brl DESC LIMIT 1"""
    assert executar_select(banco, sql)["linhas"] == [["Filme B"]]


def test_limite_de_linhas(banco):
    r = executar_select(banco, "SELECT * FROM dim_movies", max_linhas=1)
    assert len(r["linhas"]) == 1 and r["truncado"] is True


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE dim_movies",
        "DELETE FROM dim_movies",
        "UPDATE dim_movies SET titulo = 'x'",
        "PRAGMA table_info(dim_movies)",
        "SELECT 1; DROP TABLE dim_movies",
        "WITH x AS (SELECT 1) DELETE FROM dim_movies",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM alembic_version",
        "",
    ],
)
def test_bloqueia_o_que_nao_e_leitura(banco, sql):
    with pytest.raises(ConsultaInvalida):
        executar_select(banco, sql)
    assert len(executar_select(banco, "SELECT * FROM dim_movies")["linhas"]) == 2


def test_erro_de_sql_vira_mensagem(banco):
    with pytest.raises(ConsultaInvalida, match="Erro de SQL"):
        executar_select(banco, "SELECT coluna_inexistente FROM dim_movies")


def test_timeout(banco):
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
    with pytest.raises(ConsultaInvalida, match="excedeu"):
        executar_select(banco, sql, timeout=0.2)


def test_schema_lista_valores_de_baixa_cardinalidade(banco):
    schema = descrever_schema(banco)
    assert "alembic_version" not in schema
    assert "tipo_pessoa (TEXT) valores: 'Ator', 'Diretor'" in schema
    assert "sk_movie_id (TEXT) [PK]" in schema
