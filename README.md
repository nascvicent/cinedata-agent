# CineData Agent

Agente Text-to-SQL que responde perguntas em linguagem natural sobre o catálogo de filmes da
CineData Analytics, consultando a camada Gold (`cinerocket.db`, SQLite) em modo somente leitura.

Atividade de GenAI do Rocket Lab 2026 (Visagio).

## Stack

| Item | Escolha |
|---|---|
| Linguagem | Python 3.11+ |
| Framework de agentes | [PydanticAI](https://ai.pydantic.dev) |
| Modelos | Gratuitos (`:free`) via OpenRouter, com fallback automático |
| Entregável | Módulo de backend FastAPI |
| Banco | SQLite (`cinerocket.db`) |

## Como executar

1. Clone o repositório e entre na pasta:

   ```bash
   git clone <url-do-repositorio>
   cd cinedata-agent
   ```

2. Crie o ambiente virtual e instale as dependências:

   ```bash
   python -m venv .venv
   # Windows: .venv\Scripts\activate
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Baixe o `cinerocket.db` da pasta compartilhada da atividade e coloque-o na raiz do projeto
   (ao lado deste README).

4. Configure a chave do OpenRouter:

   ```bash
   # Windows: copy .env.example .env
   cp .env.example .env
   ```

   Edite o `.env` e preencha `OPENROUTER_API_KEY` com a sua chave (`sk-or-v1-...`),
   criada em <https://openrouter.ai/keys>.

5. Suba a API:

   ```bash
   uvicorn app.main:app --reload
   ```

6. Abra <http://localhost:8000> para usar a interface web. A documentação interativa da API
   fica em <http://localhost:8000/docs>. Também dá para perguntar pelo terminal:

   ```bash
   curl -X POST http://localhost:8000/perguntar \
     -H "Content-Type: application/json" \
     -d '{"pergunta": "Quais são os 10 filmes com maior receita em R$?"}'
   ```

## Rotas

| Rota | Descrição |
|---|---|
| `GET /` | Interface web: pergunta, resposta, SQL executado e tabela de dados |
| `POST /perguntar` | Recebe `{"pergunta": "..."}` e devolve a resposta, o SQL executado, as linhas retornadas, o modelo usado e quantas chamadas ao LLM foram feitas |
| `GET /schema` | Schema que o agente enxerga (gerado a partir do banco) |
| `GET /cota` | Uso da cota diária de modelos gratuitos no OpenRouter |
| `GET /health` | Verificação simples |

Exemplo de resposta de `/perguntar`:

```json
{
  "resposta": "1. ...",
  "consultas": [
    {"sql": "SELECT ...", "colunas": ["titulo", "receita_brl"], "linhas": [["...", 0.0]], "truncado": false, "erro": null}
  ],
  "modelo": "nvidia/nemotron-3.5-lightning:free",
  "requisicoes_llm": 2,
  "cache": false
}
```

## Como funciona

```
pergunta -> FastAPI -> agente (PydanticAI) -> tool executar_sql -> SQLite (read-only)
                          ^                          |
                          +------ resultado ---------+ -> resposta em português
```

- **System prompt com schema dinâmico** (`app/prompts.py`): na inicialização, o schema é lido do
  próprio banco, incluindo os valores reais de colunas categóricas como `tipo_pessoa`. O agente
  não gasta chamadas explorando tabelas. O prompt também traz o glossário de negócio
  (receita = faturamento = bilheteria, margem de lucro, "receita informada" etc.).
- **Guardrails de SQL** (`app/database.py`): conexão `mode=ro`, authorizer do SQLite que só
  permite `SELECT`, uma instrução por chamada, tabelas internas bloqueadas, limite de 50 linhas
  e timeout de 5 s por consulta.
- **Fallback entre modelos** (`app/agent.py`): se um modelo falhar (ex.: `429` por pool lotado),
  o próximo da lista `MODELOS` é tentado. Não há retry no mesmo modelo, porque requisições que
  falham também contam na cota diária.
- **Proteção da cota**: no máximo `MAX_REQUISICOES_POR_PERGUNTA` chamadas ao LLM por pergunta
  (padrão 4; o caso normal usa 2) e cache em memória de perguntas repetidas.
- **Transparência**: a resposta da API inclui o SQL executado e os dados brutos, para conferência.

## Testes

```bash
pytest
```

Os testes usam um banco temporário e um modelo falso: **não consomem requisições do OpenRouter**.
Cobrem os guardrails de SQL, a geração do schema e o fluxo completo da API (incluindo o cache).

## Estrutura

```
app/
  main.py       # rotas FastAPI
  static/index.html  # interface web (HTML único, sem build)
  agent.py      # agente, tool executar_sql e fallback de modelos
  prompts.py    # system prompt
  database.py   # acesso read-only, guardrails e introspecção do schema
  schemas.py    # modelos de entrada/saída da API
  config.py     # configurações (.env)
tests/
```

## Configuração (`.env`)

| Variável | Padrão | Descrição |
|---|---|---|
| `OPENROUTER_API_KEY` | (obrigatória) | Chave do OpenRouter |
| `MODELOS` | `nvidia/nemotron-3.5-lightning:free,z-ai/glm-5.2:free,openrouter/free` | Modelos em ordem de preferência; precisam suportar tool calling |
| `CAMINHO_BANCO` | `cinerocket.db` | Caminho do banco SQLite |
| `MAX_REQUISICOES_POR_PERGUNTA` | `4` | Teto de chamadas ao LLM por pergunta |

## Limitações

- O cache é em memória e se perde ao reiniciar o servidor.
- Não há memória de conversa: cada pergunta é independente.
- A conta gratuita do OpenRouter permite 50 requisições por dia (reset às 21h de Brasília).
