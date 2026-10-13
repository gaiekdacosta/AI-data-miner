---
titulo: "Uma frase que resume a descoberta"
perfil: vendas
tabelas: [TABELA_A, TABELA_B]
pontes:
  - TABELA_A -> TABELA_B (COLUNA_DO_JOIN)
data: 2026-01-01
---

<!--
Copie este arquivo para `NOTES/<banco>/<assunto>/<slug>.md` e apague estes
comentários. O nome do arquivo é o slug: assunto-curto-com-hifens.md.

ONDE SALVAR — a pasta é `banco/assunto`:

- O 1º nível é o **banco** (o perfil do .env, ou o `db_<perfil>_banco` dele) e
  tem de ser igual ao `perfil` do frontmatter — o `check.py` recusa se
  divergirem, porque nota no banco errado entrega schema errado ao modelo sem
  ninguém ver.
- O 2º nível é o **assunto** (vendas, estoque, financeiro, rh...). Reaproveite
  um existente (`python3 vetorial.py mapa` mostra todos); assunto novo é uma
  pasta nova, sem cerimônia.
- Achar nota não depende de saber a pasta: `python3 vetorial.py buscar
  "<pergunta>"` devolve o caminho por similaridade.

REGRAS DO CONTEÚDO:

- `tabelas`: TODAS as tabelas/views centrais da nota (alimentam o grafo e o
  pool do gerar_dicionario.py).
- `pontes`: as arestas de join que a nota descobriu, no formato
  `A -> B (colunas)` — é o que torna o grafo navegável (grafo.py caminho).
- Curto e direto (poucos parágrafos): o objetivo é acelerar a PRÓXIMA
  investigação, não documentar exaustivamente.
- Sem dado pessoal nem credencial (documento, nome de pessoa, senha) — use
  placeholders. `python3 check.py` procura o óbvio antes do commit.
- Ligue a outras notas com [[slug-da-outra-nota]] — o wikilink é pelo slug, não
  pelo caminho, então mover a nota de pasta não quebra nada.
-->

# Título da nota

**Contexto:** que pergunta se estava tentando responder.

**Caminho:** tabelas/views/colunas usadas e os JOINs/filtros que as ligam
(a "ponte" entre elas). SQL mínimo que demonstra, se ajudar.

**Lógica:** a regra de negócio por trás; por que é assim; como foi validado
(contagem, comparação com a tela do sistema etc.).

**Armadilhas:** o que parecia óbvio mas estava errado / pegadinhas que
custaram tempo.
