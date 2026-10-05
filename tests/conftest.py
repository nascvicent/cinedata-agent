"""Banco SQLite mínimo com o mesmo desenho da camada Gold, para testes sem LLM."""

import sqlite3

import pytest


@pytest.fixture
def banco(tmp_path):
    caminho = tmp_path / "teste.db"
    conn = sqlite3.connect(caminho)
    conn.executescript(
        """
        CREATE TABLE alembic_version (version_num TEXT);
        CREATE TABLE dim_movies (sk_movie_id TEXT PRIMARY KEY, titulo TEXT, ano_lancamento INTEGER);
        CREATE TABLE fact_movies_performance (sk_movie_id TEXT PRIMARY KEY, receita_brl REAL);
        CREATE TABLE dim_people (sk_person_id TEXT PRIMARY KEY, nome_pessoa TEXT, tipo_pessoa TEXT);
        INSERT INTO alembic_version VALUES ('abc');
        INSERT INTO dim_movies VALUES ('m1', 'Filme A', 2020), ('m2', 'Filme B', 2021);
        INSERT INTO fact_movies_performance VALUES ('m1', 100.0), ('m2', 300.0);
        INSERT INTO dim_people VALUES ('p1', 'Fulana', 'Ator'), ('p2', 'Beltrano', 'Diretor');
        """
    )
    conn.commit()
    conn.close()
    return str(caminho)
