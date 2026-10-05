"""Testa a lógica de comparação da avaliação (sem banco real e sem LLM)."""

from avaliacao.casos import CASOS
from avaliacao.comparar import avaliar_ranking, avaliar_tabela, texto_menciona

REF = [["A", 10.0], ["B", 9.0], ["C", 8.0], ["D", 8.0], ["E", 1.0]]


def test_ranking_aceita_colunas_extras_e_ordem_diferente():
    agente = [[1, 10, "A"], [2, 9, "B"], [3, 8, "C"]]
    assert avaliar_ranking(REF, agente, (0,), 1, 3).ok


def test_ranking_aceita_empate_na_ultima_posicao():
    agente = [["A", 10], ["B", 9], ["D", 8]]
    assert avaliar_ranking(REF, agente, (0,), 1, 3).ok


def test_ranking_rejeita_item_errado_e_linhas_faltando():
    assert not avaliar_ranking(REF, [["A", 10], ["E", 1], ["B", 9]], (0,), 1, 3).ok
    assert not avaliar_ranking(REF, [["A", 10]], (0,), 1, 3).ok


def test_ranking_com_chave_composta():
    ref = [["Ator 1", "Diretor 1", 5], ["Ator 2", "Diretor 1", 3]]
    assert avaliar_ranking(ref, [["Diretor 1", "Ator 1", 5]], (0, 1), 2, 1).ok
    assert not avaliar_ranking(ref, [["Ator 1", "Diretor 2", 5]], (0, 1), 2, 1).ok


def test_tabela_tolera_arredondamento_percentual_e_maiusculas():
    ref = [["Drama", 0.1234], [2016, 6.338]]
    agente = [["drama", 12.34], ["2016", 6.34]]
    assert avaliar_tabela(ref, agente, (0,), 1).ok


def test_tabela_rejeita_valor_divergente_ou_linha_faltando():
    ref = [["Drama", 100.0], ["Comedy", 50.0]]
    assert not avaliar_tabela(ref, [["Drama", 100.0], ["Comedy", 70.0]], (0,), 1).ok
    assert not avaliar_tabela(ref, [["Drama", 100.0]], (0,), 1).ok


def test_texto_menciona_primeiro_ou_empatado():
    ref = [["Etlb", 1.0], ["Dad, I'm Sorry", 1.0], ["Outro", 0.5]]
    assert texto_menciona("1. **Dad, I'm Sorry**: 100%", ref, (0,), 1)
    assert not texto_menciona("1. Outro", ref, (0,), 1)


def test_casos_bem_formados():
    ids = [c.id for c in CASOS]
    assert len(ids) == len(set(ids))
    for caso in CASOS:
        assert caso.tipo == "recusa" or caso.referencias, caso.id
