---
description: Responde uma pergunta de negócio consultando os bancos (somente leitura)
---

Você está em **MODO SOMENTE PERGUNTAS**. Seu objetivo é responder à pergunta de
negócio consultando os bancos — **sem alterar nada** no projeto.

## Fluxo

1. **Recupere o contexto**: `python3 recuperar.py "<termos da pergunta>"` para achar
   as tabelas e NOTES relevantes. Se vier pouco, `python3 vetorial.py buscar
   "<outros termos>"`, grep no `contexto/dicionario_<banco>.md` ou leia a nota direto.
2. **Monte o SELECT** no dialeto do perfil e valide:
   `python3 validar_sql.py --perfil <perfil> "<sql>"`.
3. **Execute** (read-only): `python3 consultar.py --perfil <perfil> query "<sql>"`
   (ou `docker compose run --rm cli --perfil <perfil> query "<sql>"`).
4. **Responda** direto ao ponto, citando a query e o perfil. Se a pergunta for
   ambígua (período, recorte), pergunte antes de escolher.

## Proibido neste chat

- Editar qualquer arquivo (`NOTES/`, `*.py`, `contexto/`, `CLAUDE.md`...).
- Rodar `gerar_dicionario.py`, `grafo.py construir` ou `vetorial.py construir`
  (escrevem arquivos). `vetorial.py buscar`/`mapa` são leitura — pode.
- Qualquer `git` que altere o repositório.

Se responder exigir uma alteração no projeto (nota nova, dicionário), **não faça** —
diga o que precisa mudar.

## Pergunta

$ARGUMENTS
