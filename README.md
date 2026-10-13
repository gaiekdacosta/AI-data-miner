# motor-consulta

Motor para um agente de IA (Claude Code, Claude Desktop ou qualquer cliente
[MCP](https://modelcontextprotocol.io)) responder perguntas de negócio consultando
**bancos de dados de produção em modo somente leitura**, sem inventar nome de
tabela e sem esquecer o que já foi descoberto.

O agente não está aqui dentro: quem pensa é o modelo do cliente. O que o projeto
entrega são as duas camadas que fazem esse agente ser útil sem ser perigoso:

- **memória**: uma base de conhecimento versionada (dicionário de dados gerado
  do catálogo + notas curtas com as regras e armadilhas de cada banco) e uma
  recuperação que entrega ao modelo só o pedaço relevante para cada pergunta;
- **contenção**: só `SELECT`/`WITH` passam, a sessão do banco é read-only onde o
  engine permite, há teto de tempo por consulta, validação de tabela e coluna antes
  de executar, acesso por pessoa e por banco, prazo de validade do acesso e trilha
  de auditoria.

Engines suportados: **PostgreSQL, MySQL/MariaDB, SQL Server, Oracle, Firebird e
SQLite**. O repositório traz um banco de exemplo (SQLite, dados fictícios) para ver
tudo funcionando em poucos minutos.

```
pergunta ─► recuperar_contexto ─► schema + notas relevantes (~1-8k tokens)
                                        │
SELECT ───► validar_sql ──────────► coluna/tabela existe?  (dicionário + catálogo ao vivo)
                                        │
            consultar_sql ────────► só SELECT/WITH · sessão read-only · teto de tempo
                                        │
                                   banco (perfil liberado para a pessoa)
```

## Experimente em 2 minutos (sem banco nenhum)

Só Python 3.10+, sem instalar pacote:

```bash
cp .env.example .env
python3 exemplo/criar_banco.py                     # gera exemplo/loja.db
python3 consultar.py ping
python3 consultar.py query "SELECT * FROM vw_faturamento_mensal LIMIT 3"
python3 recuperar.py "quanto a loja faturou por categoria em 2025"
python3 validar_sql.py "SELECT p.preco_venda FROM produtos p"
```

O `recuperar.py` mostra o que o modelo receberia antes de escrever o SQL: o schema
das tabelas certas e as notas de `NOTES/loja/`, que avisam, por exemplo, que o valor
vendido não é o preço do cadastro e que a view mensal não abate devoluções. O
`validar_sql.py` aponta a coluna inventada e sugere a certa.

Para usar com o **Claude Code** nesta máquina, sem servidor (stdio):

```bash
pip install -r requirements.txt
claude mcp add motor-consulta -- python3 "$PWD/mcp_server.py"
```

Com Docker, o mesmo exemplo:

```bash
cp .env.example .env
docker compose run --rm cli query "SELECT status, COUNT(*) FROM pedidos GROUP BY status"
```

## Ligando o seu banco

1. **Declare o perfil no `.env`.** Um perfil é um banco:

   ```ini
   PERFIS=vendas,rh
   PERFIS_SENSIVEIS=rh            # dado de pessoa: o servidor pede agregado
   db_vendas_engine=postgres
   db_vendas_host=10.0.0.10
   db_vendas_database=erp
   db_vendas_username=leitura     # SEMPRE um usuário só-leitura no banco
   db_vendas_password=...
   db_vendas_descricao=ERP — pedidos, notas fiscais e estoque
   ```

   O [.env.example](.env.example) traz o modelo de cada engine e todas as opções
   (TLS, teto de tempo, schema, dois perfis no mesmo schema).

2. **Teste a conexão:** `python3 consultar.py --perfil vendas ping` (drivers via
   `pip install -r requirements.txt`, ou use o Docker).

3. **Gere o dicionário de dados.** Banco pequeno: catálogo inteiro. Banco grande:
   só o que as notas e o `contexto/tabelas_extras_<banco>.txt` citam.

   ```bash
   python3 gerar_dicionario.py --perfil vendas --todas     # até 500 objetos
   python3 gerar_dicionario.py --perfil vendas             # pelo uso
   ```

4. **Escreva notas** à medida que descobre as regras. Cada descoberta (o filtro
   obrigatório, a ponte entre duas tabelas, a coluna que engana) vira um arquivo
   curto em `NOTES/<banco>/<assunto>/`, partindo de
   [NOTES/_TEMPLATE.md](NOTES/_TEMPLATE.md). Depois, `python3 check.py`.

5. Apague o exemplo quando não precisar mais: `NOTES/loja/`,
   `contexto/dicionario_loja.md`, `contexto/cabecalho_loja.md`, o perfil `loja` do
   `.env` e as linhas do exemplo em `contexto/instrucoes.md` e
   `contexto/glossario.txt`. Rode `python3 check.py` para regenerar os índices.

## Servidor compartilhado (uma base, várias pessoas)

Para a equipe inteira usar o mesmo conhecimento, sobem três serviços: o servidor
MCP, um proxy Caddy que exige token por pessoa e um painel web para administrar
esses tokens.

```bash
cp .env.example .env                        # perfis + MCP_PROXY_SEGREDO
cp .env.admin.example .env.admin            # ADMIN_SENHA
cp caddy/tokens.caddy.example caddy/tokens.caddy
chmod 600 .env .env.admin caddy/tokens.caddy
docker compose up -d mcp proxy admin
```

No painel (`http://127.0.0.1:8012`) você cria o acesso de cada pessoa, escolhe os
bancos dela e recebe o comando pronto para o Claude Code e o bloco de configuração
do Claude Desktop. O passo a passo completo, com rede, TLS, prazos e operação, está
em [docs/IMPLANTACAO.md](docs/IMPLANTACAO.md).

| Tool / recurso | O que faz |
|---|---|
| `listar_indice()` | índice global: tabelas conhecidas + notas, com caminho |
| `recuperar_contexto(pergunta)` | schema + notas relevantes para a pergunta |
| `validar_sql(sql, perfil)` | confere coluna e tabela antes de executar |
| `consultar_sql(sql, perfil)` | executa o SELECT e devolve CSV |
| `nota://<banco>/<assunto>/<slug>` | cada nota como resource |
| `dicionario://<perfil>/<tabela>` | schema de uma tabela |
| prompt `pergunta` | o roteiro recuperar → validar → consultar → citar a query |

## Segurança, em uma tabela

| Camada | O que garante |
|---|---|
| `consultar.py` | só uma instrução, começando por `SELECT`/`WITH`, sem comando de escrita no meio (inclusive CTE com `DELETE`) |
| sessão do banco | read-only no PostgreSQL, MySQL/MariaDB e SQLite (o servidor recusa escrita mesmo de uma conta dona) |
| teto de tempo | no servidor (MySQL, PostgreSQL) e por cancelamento (Oracle, Firebird, SQLite); SQL Server pelo driver |
| proxy Caddy | token por pessoa; injeta os bancos liberados, quem é e até quando vale |
| segredo do proxy | o servidor MCP recusa (403) qualquer HTTP que não passou pelo proxy |
| painel | não recebe as senhas dos bancos; sem `ADMIN_SENHA` só sobe em loopback |
| trilha | toda consulta (e toda recusa) em `auditoria/`, com pessoa, perfil e SQL |

A camada que mais importa continua sendo a sua: **use um usuário de banco que só
pode ler**. A ferramenta é read-only por construção, mas a credencial deveria ser
também.

## Mapa do repositório

| Arquivo | Papel |
|---|---|
| [perfis.py](perfis.py) | registro dos perfis declarados no `.env` |
| [conexao.py](conexao.py) | conexão read-only nos 6 engines |
| [consultar.py](consultar.py) | CLI e o único caminho de execução de SQL |
| [validar_sql.py](validar_sql.py) | guardrail de schema |
| [gerar_dicionario.py](gerar_dicionario.py) | catálogo do banco → `contexto/dicionario_<banco>.md` |
| [grafo.py](grafo.py), [vetorial.py](vetorial.py), [recuperar.py](recuperar.py) | grafo das notas, índice TF-IDF e recuperação |
| [check.py](check.py) | verificador da base (rode antes de commitar; roda no CI) |
| [mcp_server.py](mcp_server.py) | servidor MCP |
| [admin_app.py](admin_app.py), [admin_tokens.py](admin_tokens.py) | painel de acessos |
| [auditoria.py](auditoria.py), [perguntas.py](perguntas.py) | trilha de consultas e catálogo de perguntas (fila de lacunas) |
| [ARQUITETURA.md](ARQUITETURA.md) | por que cada peça é como é |

Testes: `python3 -m unittest discover -s tests -v` (biblioteca padrão; o teste do
painel roda quando `httpx` está instalado).
