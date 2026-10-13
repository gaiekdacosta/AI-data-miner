# motor-consulta — regras para o Claude Code

Ferramenta de investigação SOMENTE LEITURA nos bancos declarados no `.env`
(`PERFIS`). `python3 consultar.py perfis` lista o que está configurado, com engine e
descrição. Arquitetura em `ARQUITETURA.md`.

## Como executar consultas

```bash
python3 consultar.py query "SELECT ... LIMIT 5"                 # perfil padrão
python3 consultar.py --perfil rh query "SELECT TOP 5 ..."
python3 consultar.py tabelas "PED%"
python3 consultar.py describe pedidos
docker compose run --rm cli --perfil vendas query "SELECT ..."  # mesmo, no Docker
```

Use o dialeto do engine do perfil (o dicionário de cada banco diz qual). Nada além
de SELECT/WITH passa pela validação, e consultas têm teto de tempo — agregue e
filtre cedo.

## Antes de explorar o schema — consulte a base local

Um dicionário por banco em `contexto/dicionario_<banco>.md`. **Procure lá primeiro**
(e não carregue o arquivo inteiro: use grep ou leitura pontual), em vez de rodar
`tabelas`/`describe`/`amostra` contra o banco:

```bash
python3 recuperar.py "faturamento por categoria"   # schema + notas da pergunta
python3 vetorial.py buscar "devolução"             # onde está anotado
grep -n -A 20 "### pedidos " contexto/dicionario_loja.md
python3 grafo.py caminho clientes produtos         # como ligar duas tabelas
```

Só vá ao banco quando a tabela não estiver no dicionário — e então regenere:

```bash
python3 gerar_dicionario.py --perfil <perfil>
```

## Valide antes de executar query nova

```bash
python3 validar_sql.py --perfil <perfil> "SELECT ..."
```

## Descobertas viram NOTES (obrigatório)

Toda descoberta relevante (ponte entre tabelas, regra de negócio, armadilha) vira
nota curta em `NOTES/<banco>/<assunto>/<slug>.md`, partindo de
`NOTES/_TEMPLATE.md`. O 1º nível da pasta é o banco e tem de ser igual ao `perfil`
do frontmatter. Declare `tabelas:` e `pontes:` (`A -> B (colunas)`).

Sem dado pessoal nem credencial nas notas. Antes de commitar:

```bash
python3 check.py                               # frontmatter, índices, vazamentos
python3 -m unittest discover -s tests          # testes
```

## Limites

- Nunca escreva no banco, nem para "testar": a ferramenta não deixa, e não há
  caminho alternativo.
- Perfis em `PERFIS_SENSIVEIS` têm dado de pessoa: traga agregado.
- Cite sempre a query e o perfil usados. Número sem query não se confere.
