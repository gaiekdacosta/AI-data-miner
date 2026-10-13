#!/usr/bin/env python3
"""Guardrail de schema — check pré-voo de um SELECT antes de rodá-lo.

Valida os identificadores de um SQL contra o dicionário de dados do perfil
(``contexto/dicionario_<banco>.md``) para pegar tabela/coluna inexistente
(alucinação) ANTES de executar. Pensado como ferramenta do agente: rode este
check, corrija o que ele apontar, e só então mande para ``consultar.py``.

Duas verificações, com fontes diferentes:
  - **colunas** qualificadas (``alias.COL``) cujo dono é uma tabela DESCRITA no
    dicionário → conferidas contra a lista de colunas (estático, sem banco);
  - **tabelas** não catalogadas → confirmadas ao vivo no catálogo do banco (1
    query, e só quando aparece uma tabela desconhecida).

Alta precisão de propósito: na dúvida, LIBERA (nunca acusa query válida). Onde ele
mais paga é em banco de nome cifrado (tabelas de 3 letras, colunas abreviadas):
ali o modelo inventa coluna e ninguém avisa.

Uso:

    python3 validar_sql.py "SELECT p.total FROM pedidos p"
    python3 validar_sql.py --perfil rh -f consulta.sql

Sem driver de banco instalado, a checagem de colunas funciona; a de existência de
tabela é pulada (fail-open) por não haver conexão.
"""

import argparse
import difflib
import os
import re
import sys

try:  # como pacote / como script
    from . import perfis as _perfis
except ImportError:
    import perfis as _perfis

_AQUI = os.path.dirname(os.path.abspath(__file__))

_KEYWORDS = {
    "SELECT", "FROM", "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER",
    "CROSS", "ON", "USING", "AND", "OR", "NOT", "AS", "GROUP", "ORDER", "BY",
    "HAVING", "UNION", "ALL", "MINUS", "INTERSECT", "WITH", "PARTITION", "OVER",
    "FETCH", "OFFSET", "ROWS", "START", "CONNECT", "LATERAL", "TABLE", "DUAL",
    "IN", "EXISTS", "BETWEEN", "LIKE", "CASE", "WHEN", "THEN", "ELSE", "END",
    "DISTINCT", "ASC", "DESC", "NULL", "IS",
}
_PSEUDO_COL = {"ROWNUM", "ROWID", "NEXTVAL", "CURRVAL", "LEVEL", "OID", "CTID"}
# Catálogo de cada engine: consultar isso é legítimo e não é "tabela inexistente".
_SISTEMA_POR_ENGINE = {
    "oracle": ("ALL_", "USER_", "DBA_", "GV$", "V$", "X$", "SYS_"),
    "sqlserver": ("SYS.", "SYSOBJECTS", "SYSCOLUMNS", "INFORMATION_SCHEMA", "#", "##"),
    "firebird": ("RDB$", "MON$", "SEC$"),
    "mysql": ("INFORMATION_SCHEMA", "PERFORMANCE_SCHEMA", "MYSQL.", "SYS."),
    "postgres": ("PG_", "INFORMATION_SCHEMA"),
    "sqlite": ("SQLITE_", "PRAGMA_"),
}

_STR_RE = re.compile(r"'(?:[^']|'')*'")
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_FROM_JOIN_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?)"
    r"(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?", re.IGNORECASE)
_AS_PAREN_RE = re.compile(r"\b([A-Za-z_]\w*)\s+AS\s*\(", re.IGNORECASE)  # CTE/subquery
_QUALCOL_RE = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")


def dicionario_do_perfil(perfil=None):
    """Caminho do dicionário do perfil (pode não existir ainda)."""
    banco = _perfis.banco(_perfis.conferir(perfil))
    return os.path.join(_AQUI, "contexto", f"dicionario_{banco}.md")


def carregar_catalogo(perfil=None, texto_dicionario=None):
    """{TABELA: {COLUNAS}} a partir dos cabeçalhos '### TAB (' e das tabelas
    markdown do dicionário do perfil. Para na seção de NOTES (que também usa '###').

    Dicionário ausente devolve {} — e catálogo vazio faz o guardrail liberar tudo
    (fail-open): ele nunca é a última linha de defesa."""
    if texto_dicionario is None:
        caminho = dicionario_do_perfil(perfil)
        if not os.path.isfile(caminho):
            return {}
        with open(caminho, encoding="utf-8") as fh:
            texto_dicionario = fh.read()
    catalogo = {}
    atual = None
    for linha in texto_dicionario.splitlines():
        if linha.startswith("## Conhecimento"):
            break
        m = re.match(r"^###\s+([A-Za-z0-9_$#]+)\s+\(", linha)
        if m:
            atual = m.group(1).upper()
            catalogo.setdefault(atual, set())
            continue
        if atual and linha.startswith("|"):
            cel = [c.strip() for c in linha.strip().strip("|").split("|")]
            if not cel:
                continue
            prim = cel[0]
            if prim == "Coluna" or set(prim) <= set("-: "):  # cabeçalho/separador
                continue
            if re.match(r"^[A-Za-z_][\w$#]*$", prim):
                catalogo[atual].add(prim.upper())
    return catalogo


def _limpar_sql(sql):
    """Remove comentários e literais de string para não casar identificadores lá dentro."""
    s = _BLOCK_COMMENT_RE.sub(" ", sql)
    s = _LINE_COMMENT_RE.sub(" ", s)
    s = _STR_RE.sub("''", s)
    return s


def _eh_tabela_sistema(nome, engine):
    up = nome.upper()
    if up in ("DUAL", "RDB$DATABASE"):
        return True
    return up.startswith(_SISTEMA_POR_ENGINE.get(engine, ()))


def _extrair(limpo):
    """Devolve (tabelas, alias_map, derivadas, qualificadas) do SQL já limpo.

    ``qualificadas`` é {TABELA: {"SCHEMA.TABELA", ...}} para os nomes escritos com
    prefixo: é com o prefixo que se reconhece catálogo de sistema
    (``information_schema.tables`` não é a tabela TABLES do usuário).
    """
    derivadas = {m.group(1).upper() for m in _AS_PAREN_RE.finditer(limpo)}
    tabelas, alias_map, qualificadas = set(), {}, {}
    for m in _FROM_JOIN_RE.finditer(limpo):
        completo = m.group(1).upper()
        tab = completo.split(".")[-1]
        if tab in _KEYWORDS:  # ex.: FROM TABLE(func(...)), FROM LATERAL (...)
            continue
        tabelas.add(tab)
        if "." in completo:
            qualificadas.setdefault(tab, set()).add(completo)
        alias = m.group(2)
        if alias and alias.upper() not in _KEYWORDS:
            alias_map[alias.upper()] = tab
    tabelas -= derivadas
    return tabelas, alias_map, derivadas, qualificadas


def _tab_do_qualificador(q, alias_map, catalogo):
    up = q.upper()
    if up in alias_map:
        return alias_map[up]
    if up in catalogo:
        return up
    return None


def _existem_no_banco(nomes, perfil):
    """Quais dos nomes existem como objeto no banco do perfil (1 query).

    Cada engine tem seu catálogo. É esta checagem que cobre o que o dicionário não
    cataloga — em banco com milhares de tabelas o dicionário é semente, não
    catálogo inteiro, e ela faz o trabalho pesado.
    """
    try:  # como pacote / como script
        from .conexao import create_readonly_connection, engine_do_perfil
    except ImportError:
        from conexao import create_readonly_connection, engine_do_perfil

    engine = engine_do_perfil(perfil)
    conn = create_readonly_connection(perfil)
    try:
        cur = conn.cursor()
        try:
            if engine in ("mysql", "sqlserver", "postgres"):
                marcadores = ", ".join(["%s"] * len(nomes))
                params = tuple(n.upper() for n in nomes)
                if engine == "mysql":
                    sql = (f"SELECT TABLE_NAME FROM information_schema.tables "
                           f"WHERE TABLE_SCHEMA = DATABASE() "
                           f"AND UPPER(TABLE_NAME) IN ({marcadores})")
                elif engine == "sqlserver":
                    sql = (f"SELECT name FROM sys.objects "
                           f"WHERE type IN ('U','V','SN','IF','TF') "
                           f"AND UPPER(name) IN ({marcadores})")
                else:
                    # `relkind` cobre tabela, view, view materializada, tabela
                    # particionada e tabela externa — tudo que um SELECT alcança.
                    sql = (f"SELECT c.relname FROM pg_class c "
                           f"JOIN pg_namespace n ON n.oid = c.relnamespace "
                           f"WHERE n.nspname = current_schema() "
                           f"AND c.relkind IN ('r','v','m','p','f') "
                           f"AND UPPER(c.relname) IN ({marcadores})")
                cur.execute(sql, params)
            elif engine == "sqlite":
                marcadores = ", ".join(["?"] * len(nomes))
                cur.execute(
                    f"SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                    f"AND UPPER(name) IN ({marcadores})",
                    tuple(n.upper() for n in nomes),
                )
            elif engine == "firebird":
                # Identificador do Firebird cabe em 31 chars; parâmetro mais longo
                # faz o servidor estourar com "string right truncation".
                curtos = [n.upper() for n in nomes if len(n) <= 31]
                if not curtos:
                    return set()
                marcadores = ", ".join(["?"] * len(curtos))
                cur.execute(
                    f"SELECT TRIM(rdb$relation_name) FROM rdb$relations "
                    f"WHERE UPPER(TRIM(rdb$relation_name)) IN ({marcadores})",
                    curtos,
                )
            else:
                binds = {f"n{i}": nome for i, nome in enumerate(nomes)}
                lista = ", ".join(f":{k}" for k in binds)
                cur.execute(
                    f"SELECT object_name FROM all_objects "
                    f"WHERE object_name IN ({lista}) "
                    f"AND object_type IN ('TABLE','VIEW','SYNONYM','MATERIALIZED VIEW')",
                    binds,
                )
            return {r[0].upper() for r in cur.fetchall() if r[0]}
        finally:
            try:
                cur.close()
            except Exception:  # fdb fecha junto com a conexão
                pass
    finally:
        conn.close()


def validar_schema(sql, catalogo, perfil=None):
    """Lista de problemas de schema (vazia = ok). Fail-open em qualquer erro.

    Sem catálogo (dicionário do perfil ainda não gerado) libera tudo: o guardrail
    é um atalho para o modelo se corrigir, não a trava de segurança — essa é o
    ``_validar_select`` (só SELECT/WITH) e o gate de perfil por pessoa.
    """
    if not catalogo:
        return []
    try:
        engine = _perfis.engine(perfil)
    except Exception:
        engine = "?"
    try:
        limpo = _limpar_sql(sql)
        tabelas, alias_map, derivadas, qualificadas = _extrair(limpo)
        qualcols = _QUALCOL_RE.findall(limpo)
    except Exception as e:  # parser nunca deve barrar query válida
        print(f"  [guardrail] parse falhou, liberando: {e}", file=sys.stderr)
        return []

    problemas = []

    # Colunas qualificadas cujo dono é uma tabela DESCRITA (colunas completas).
    for q, col in qualcols:
        tab = _tab_do_qualificador(q, alias_map, catalogo)
        if tab and tab in catalogo and catalogo[tab]:
            if col.upper() not in catalogo[tab] and col.upper() not in _PSEUDO_COL:
                sug = difflib.get_close_matches(col.upper(), list(catalogo[tab]), n=3)
                dica = f" — talvez: {', '.join(sug)}" if sug else ""
                problemas.append(f"coluna {q}.{col} não existe em {tab}{dica}")

    # Tabelas não catalogadas: confirma no banco (só quando há desconhecidas).
    # Nome com prefixo de schema não é acusado no MySQL/PostgreSQL: lá o catálogo
    # é consultado só no schema corrente, e `outro_schema.tabela` sairia como
    # inexistente sendo válida — na dúvida, libera.
    def _de_sistema(t):
        return _eh_tabela_sistema(t, engine) or any(
            _eh_tabela_sistema(q, engine) for q in qualificadas.get(t, ()))

    desconhecidas = sorted(
        t for t in tabelas
        if t not in catalogo and t not in derivadas and not _de_sistema(t)
        and not (engine in ("mysql", "postgres") and t in qualificadas)
    )
    if desconhecidas:
        try:
            existem = _existem_no_banco(desconhecidas, perfil)
        except Exception as e:
            print(f"  [guardrail] checagem de existência pulada (sem banco): {e}", file=sys.stderr)
            existem = set(desconhecidas)  # fail-open
        for t in desconhecidas:
            if t not in existem:
                sug = difflib.get_close_matches(t, list(catalogo), n=3)
                dica = f" — talvez: {', '.join(sug)}" if sug else ""
                problemas.append(f"tabela {t} não existe{dica}")

    # dedup preservando ordem
    saida = []
    for p in problemas:
        if p not in saida:
            saida.append(p)
    return saida


def main(argv=None):
    p = argparse.ArgumentParser(description="Valida os identificadores de um SELECT contra o dicionário.")
    p.add_argument("sql", nargs="?", help="a consulta (ou use -f)")
    p.add_argument("-f", "--arquivo", help="lê a consulta de um arquivo .sql")
    p.add_argument("--perfil", default=None,
                   help=f"perfil (padrão: {_perfis.PERFIL_PADRAO or '—'}; "
                        f"configurados: {', '.join(_perfis.PERFIS) or 'nenhum'})")
    args = p.parse_args(argv)

    if args.arquivo:
        with open(args.arquivo, encoding="utf-8") as fh:
            sql = fh.read()
    elif args.sql:
        sql = args.sql
    else:
        p.error("informe o SQL como argumento ou via -f arquivo.sql")

    try:
        perfil = _perfis.conferir(args.perfil)
    except ValueError as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 2
    caminho = dicionario_do_perfil(perfil)
    if not os.path.isfile(caminho):
        print(f"{os.path.relpath(caminho, _AQUI)} não existe — rode "
              f"gerar_dicionario.py --perfil {perfil}.", file=sys.stderr)
        return 2

    problemas = validar_schema(sql, carregar_catalogo(perfil), perfil)
    if not problemas:
        print("OK — nenhum identificador desconhecido no schema.")
        return 0
    print("PROBLEMAS de schema (corrija antes de executar):")
    for p_ in problemas:
        print(f"  - {p_}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
