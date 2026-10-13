# Arquitetura

Como as peças se encaixam e **por que** foram decididas assim. Para usar, comece
pelo [README.md](README.md); para subir o servidor, [docs/IMPLANTACAO.md](docs/IMPLANTACAO.md).

## 1. O que o projeto é

Uma ferramenta de investigação **somente leitura** sobre bancos de dados,
acoplada a uma **base de conhecimento versionada** que acumula o que já se
descobriu sobre eles. O agente é o cliente MCP (Claude Code, Claude Desktop); o
projeto entrega **memória** (para não redescobrir a mesma ponte entre tabelas toda
semana) e **contenção** (para não escrever nada, não vazar dado de pessoa e não
inventar nome de coluna).

## 2. Camadas

```
                          ┌──────────────────────────────────────────┐
  pergunta ──────────────►│  RECUPERAÇÃO   recuperar.py              │
                          │  vetorial.py → grafo.py → render         │
                          └───────────────┬──────────────────────────┘
                                          │ nomes reais de tabela/coluna
                                          ▼
                          ┌──────────────────────────────────────────┐
  SELECT ────────────────►│  GUARDRAIL     validar_sql.py            │
                          │  coluna vs dicionário                    │
                          │  tabela vs catálogo ao vivo              │
                          └───────────────┬──────────────────────────┘
                                          ▼
                          ┌──────────────────────────────────────────┐
                          │  EXECUÇÃO      consultar.py + conexao.py │
                          │  _validar_select → _executar (tetos)     │
                          └───────────────┬──────────────────────────┘
                                          ▼
                                bancos (read-only), um por perfil
```

**Configuração.** [perfis.py](perfis.py) lê do `.env` quais bancos existem
(`PERFIS`) e o que não é segredo de cada um (engine, descrição, sensibilidade,
banco de conhecimento). Usuário e senha só são lidos por [conexao.py](conexao.py)
na hora de conectar. A separação permite que o painel e as ferramentas da base de
conhecimento rodem sem driver e sem credencial.

**Execução.** [consultar.py](consultar.py) é a CLI e o **único** caminho até o
banco: `_validar_select` recusa tudo que não seja uma instrução `SELECT`/`WITH`
(inclusive `WITH ... DELETE`), e `_executar` aplica o teto de linhas e o de tempo.
O servidor MCP reusa essas mesmas funções — não existe segundo caminho.

A trava da aplicação é reforçada pela sessão do banco onde o engine permite:
`default_transaction_read_only` no PostgreSQL, `SET SESSION TRANSACTION READ ONLY`
no MySQL/MariaDB, `mode=ro` + `query_only` no SQLite. Com isso o servidor recusa
escrita mesmo vinda de uma conta que poderia escrever.

O teto de tempo tem duas metades. Onde o servidor sabe cortar
(`statement_timeout`, `MAX_EXECUTION_TIME`/`max_statement_time`), ele corta. Onde
não sabe (Oracle, Firebird, SQLite), um vigia (`_Watchdog`) cancela a consulta pela
API do driver quando o teto passa — cobrindo execute **e** fetch, porque o
travamento típico é no fetch.

## 3. Base de conhecimento

A fonte da verdade é **markdown**. Todo o resto é derivado e regenerável:

```
NOTES/<banco>/<assunto>/*.md   ──┬──►  contexto/dicionario_<banco>.md  (PRECISA do banco)
                                 ├──►  contexto/notes.json             (índice das notas)
                                 ├──►  contexto/edges.json             (grafo, node-link)
                                 └──►  contexto/vetores.json           (TF-IDF esparso)

contexto/glossario.txt             escrito à mão (grupos de sinônimos)
contexto/cabecalho_<banco>.md      escrito à mão (regras do banco, topo do dicionário)
contexto/tabelas_extras[_<banco>].txt  escrito à mão (nomes que nenhum regex acha)
contexto/instrucoes.md             escrito à mão (regras do domínio para o servidor MCP)
```

**Uma nota por descoberta**, curta, em quatro blocos: Contexto, Caminho (tabelas e
JOINs), Lógica (a regra e como foi validada), Armadilhas. O frontmatter declara as
`tabelas` e as `pontes` (`A -> B (colunas)`), que viram as arestas do grafo.

O 1º nível da pasta é o **banco** e tem de ser igual ao `perfil` do frontmatter —
o `check.py` recusa se divergirem. O motivo é concreto: a mesma sigla de tabela
pode existir em dois bancos com colunas diferentes, e nota no banco errado entrega
o schema errado ao modelo sem ninguém perceber. Dois perfis que enxergam o mesmo
schema compartilham o banco de conhecimento (`db_<perfil>_banco`).

**Dicionário pelo uso, não pelo catálogo inteiro.** Num ERP de milhares de
tabelas, catalogar tudo produziria centenas de milhares de tokens que nenhum modelo
deveria ler. O `gerar_dicionario.py` descreve os candidatos que as notas citam
(frontmatter e `FROM`/`JOIN` dos exemplos), as views do banco e os extras; o banco
descarta o que não existe nele. O que o dicionário não cobre, a checagem de
existência ao vivo do guardrail cobre. Para banco pequeno, `--todas`.

## 4. Recuperação

[recuperar.py](recuperar.py) devolve o subgrafo da pergunta em três etapas:

1. **Sementes pelo índice vetorial.** [vetorial.py](vetorial.py) mantém um vetor
   TF-IDF por nota e por tabela, com o caminho do arquivo ao lado. Python puro, sem
   API e sem embedding externo: o arquivo é determinístico e versionável. Cada
   palavra entra como **radical** (plural/flexão do português normalizados) e como
   **prefixo de 5 letras** (casa abreviação com palavra inteira); o IDF decide qual
   forma manda. A cauda fraca do ranking é cortada a 30% do topo.
2. **Expansão pelo grafo** ([grafo.py](grafo.py)): tabelas-ponte a 1 salto, tabelas
   citadas pelas notas e notas ligadas por `[[wikilink]]`.
3. **Render:** schema compacto (`coluna (tipo)`) com o rótulo do perfil a usar +
   corpo das notas, cada bloco marcado com o caminho de origem.

**Sinônimo é vocabulário, não morfologia**: radical nenhum liga `devolução` a
`estorno`. Quem faz isso é o `glossario.txt`, aplicado só do lado da pergunta —
grupo novo não exige reconstruir o índice. Trocar o TF-IDF por embeddings mexe só na
etapa 1.

## 5. O servidor MCP

| Primitiva | O que é | Anotação |
|---|---|---|
| `listar_indice()` | tool — índice global | `readOnly`, local |
| `recuperar_contexto(pergunta)` | tool — subgrafo relevante | `readOnly`, local |
| `validar_sql(sql, perfil)` | tool — guardrail | `readOnly`, `openWorld` |
| `consultar_sql(sql, perfil, max_linhas)` | tool — executa, devolve CSV | `readOnly`, `openWorld` |
| `nota://<banco>/<assunto>/<slug>` | uma resource por nota | `text/markdown` |
| `dicionario://<perfil>/<tabela>` | template de schema | `text/markdown` |
| `pergunta(pergunta)` | prompt — o roteiro | — |

As `instructions` do servidor (que o cliente injeta em toda sessão) listam os
perfis, marcam os sensíveis e trazem o `contexto/instrucoes.md`. São curtas de
propósito: custam token em toda sessão.

**Falha é `isError`** (via `ToolError`), não texto de erro numa resposta de
sucesso, que o modelo às vezes lê como dado. Exceção proposital: `validar_sql`
achando problema é sucesso — é o trabalho dela.

**Consulta fora do event loop.** O FastMCP chama tool síncrona inline no loop; uma
consulta pendurada pararia o servidor para todos. `consultar_sql` roda o executor
numa thread (`anyio.to_thread`), e o vigia de tempo encerra a consulta — as duas
metades são necessárias.

**Sem sessão MCP** (`stateless_http`). Com sessão, um stream SSE fica aberto por
horas; quando a rede corta esse TCP sem aviso, a reconexão do cliente leva 409 e a
ferramenta some até o app reiniciar. As tools são síncronas e o servidor nunca
empurra nada, então a sessão não faz falta.

## 6. Quem pode consultar o quê

O servidor MCP não autentica pessoa. Quem autentica é o **proxy Caddy**, com um
Bearer por pessoa, e é ele que diz ao servidor quem é, que bancos alcança e até
quando, por headers que injeta com `header_up` (SET: o valor que o cliente mandar é
sobrescrito).

Isso só vale para quem passa pelo proxy. Quem alcançasse a porta do servidor MCP
direto (outro container da rede, um processo do host) mandaria `X-Db-Perfis: *` e
consultaria tudo. Por isso o Caddyfile injeta um **segredo** em toda requisição e o
servidor recusa (403) qualquer HTTP sem ele. O gate fica no transporte, não em cada
tool: cobre tools, resources, prompts e o `initialize`, e nenhuma tool futura
precisa lembrar de checar.

Toda ausência de configuração nega em vez de liberar: segredo vazio = HTTP fechado;
`MCP_PERFIS_PADRAO=""` = requisição sem atribuição não consulta nada; painel sem
senha só sobe em loopback.

**Prazo.** Cada bloco de pessoa carrega `X-Db-Expira`; depois da data, as tools de
dados recusam com uma mensagem que diz o que pedir (renovação). Token sem prazo
mantém válido para sempre qualquer vazamento antigo. *Renovar* estende sem trocar o
token; *rotacionar* troca e renova. A base de conhecimento continua acessível — o
que o prazo protege é o dado.

**O painel** ([admin_app.py](admin_app.py), [admin_tokens.py](admin_tokens.py))
edita o `tokens.caddy` **in place** (o proxy o monta como arquivo único; um rename
trocaria o inode e o Caddy seguiria lendo o antigo), recarrega o Caddy pelo **socket
unix** de admin (numa porta TCP, qualquer container da rede poderia reconfigurar o
gate) e faz tudo de forma transacional: se o Caddy recusar a configuração, volta a
anterior. Uma trava de contagem impede que uma operação mude mais acessos do que
deveria (criar = +1, revogar = −1, o resto = 0). O painel não recebe as senhas dos
bancos.

## 7. Duas gravações que não se misturam

| | [auditoria.py](auditoria.py) | [perguntas.py](perguntas.py) |
|---|---|---|
| responde | quem consultou o quê | o que perguntam e o que a base não sabe |
| grava | todo SQL, inclusive recusa e erro | a pergunta, o que a recuperação entregou e a query que funcionou |
| serve para | prestar contas | escrever a próxima nota |

O catálogo de perguntas guarda a **força** de cada recuperação (o score da melhor
nota). Sem ela, "recuperou" seria sempre verdadeiro — o cosseno devolve alguma nota
para qualquer texto — e a fila de lacunas nunca dispararia. Abaixo de
`FORCA_MINIMA` a pergunta entra na fila; o `gerar_dossie.sh` transforma cada lacuna
num rascunho de nota (frontmatter preenchido + as SQL que funcionaram). Escrever a
nota continua sendo trabalho de julgamento: a SQL registrada rodou, o que não quer
dizer que a leitura dela estava certa.

Os dois arquivos contêm dado sensível (um `WHERE` pode ter documento; uma pergunta
tem nome de cliente): ficam fora do git, com permissão restrita, um arquivo por dia.

## 8. Verificação

[check.py](check.py) roda antes do commit e no CI, sem banco e sem dependências:
pasta e frontmatter de cada nota, formato das pontes, wikilinks, sincronia dos
índices derivados e vazamentos óbvios (CPF, senha em texto). Imprime também a
**cobertura** — tabela citada sem schema no dicionário do próprio banco, nota sem
pontes —, que é a fila de trabalho, não erro.

Os testes em `tests/` cobrem o validador, o registro de perfis, o guardrail, o
léxico, o ponta a ponta no SQLite de exemplo (inclusive o teto de tempo e a trava
do próprio banco) e o ciclo de vida de um token no painel.
