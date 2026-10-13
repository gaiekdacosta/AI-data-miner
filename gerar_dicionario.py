#!/usr/bin/env python3
"""Gera o dicionário de dados de um perfil e os índices da base de conhecimento.

Varre as anotações (``NOTES/**/*.md``) atrás de nomes de tabelas/views, descreve
cada uma **no banco do perfil** (colunas, tipos e comentários) e escreve
``contexto/dicionario_<banco>.md``. Com ``--todas``, cataloga o schema inteiro em
vez dos candidatos — o certo para banco pequeno; num ERP de milhares de tabelas,
não.

Um pool de candidatos só, um catálogo por banco: cada engine descreve o que existe
nele e descarta o resto em silêncio. É o que torna isto seguro em banco enorme de
nome cifrado: cataloga-se o que as NOTES realmente usam, e a checagem de
existência ao vivo do ``validar_sql.py`` cobre o resto.

Ao final, regenera o grafo (notes.json + edges.json) e o índice vetorial
(vetores.json), que leem todos os dicionários.

O arquivo gerado é determinístico (tabelas em ordem alfabética) — diffs limpos no
git e consumo estável pelo ``recuperar.py``/``grafo.py``.

Uso (mesmo ambiente da CLI — no Docker ou com os drivers instalados):

    python3 gerar_dicionario.py                        # perfil padrão, candidatos das NOTES
    python3 gerar_dicionario.py --perfil rh            # outro perfil
    python3 gerar_dicionario.py --perfil loja --todas  # o schema inteiro
    python3 gerar_dicionario.py pedidos clientes       # inclui tabelas avulsas

Tabelas extras permanentes: uma por linha em ``contexto/tabelas_extras.txt``
(vale para todos) ou ``contexto/tabelas_extras_<banco>.txt`` (só aquele banco).
Regras do banco escritas à mão (o que o catálogo não diz: filtros obrigatórios,
tabelas a evitar, convenções de nome) vão em ``contexto/cabecalho_<banco>.md``, que
entra no topo do dicionário.
"""

import argparse
import os
import re
import sys
from contextlib import contextmanager

try:  # como pacote
    from .conexao import create_readonly_connection, engine_do_perfil
    from . import grafo, vetorial
    from . import perfis as _perfis
except ImportError:  # como script
    from conexao import create_readonly_connection, engine_do_perfil
    import grafo
    import vetorial
    import perfis as _perfis

_AQUI = os.path.dirname(os.path.abspath(__file__))
_NOTES = os.path.join(_AQUI, "NOTES")
_CONTEXTO = os.path.join(_AQUI, "contexto")
_EXTRAS = os.path.join(_CONTEXTO, "tabelas_extras.txt")

# Identificador depois de FROM/JOIN no SQL de exemplo das notas — é o que pega
# nome de tabela que nenhum padrão adivinha.
_RE_FROM_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+(?:[A-Z_][A-Z0-9_$#]*\.)?([A-Z_][A-Z0-9_$#]*)", re.I)
_RE_FM_TABELAS = re.compile(r"^tabelas:\s*\[([^\]]*)\]", re.M)
_RE_FM_PONTES = re.compile(r"^\s*-\s*([A-Z_][A-Z0-9_$#]*)\s*->\s*([A-Z_][A-Z0-9_$#]*)", re.M | re.I)
_RE_COMENTARIO_SQL = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

# Palavras que aparecem depois de FROM/JOIN mas não são tabela.
_KEYWORDS_SQL = {
    "SELECT", "TABLE", "LATERAL", "DUAL", "OPENJSON", "OPENQUERY", "VALUES",
    "INSERTED", "DELETED", "SYSDATE", "UNNEST", "GENERATE_SERIES",
}


def _extras_do_banco(banco):
    return os.path.join(_CONTEXTO, f"tabelas_extras_{banco}.txt")


def _candidatos(argv_extras, banco):
    """Coleta nomes candidatos das NOTES e dos extras.

    Fontes, porque uma só não basta:
      - o identificador depois de FROM/JOIN no SQL de exemplo das notas
        (comentário removido antes, senão "-- NAO USAR" entraria como tabela);
      - o frontmatter das NOTES (``tabelas:`` e os dois lados de cada ``ponte``);
      - ``contexto/tabelas_extras[_<banco>].txt`` e os nomes passados na linha de
        comando, lidos como LISTA LITERAL (um por linha) — é o único caminho para
        nome que nenhum regex acha.

    Não se tenta adivinhar a que banco cada nome pertence: quem decide é o banco,
    ao descrever (o que não existe nele é descartado).
    """
    textos = []
    for raiz, dirs, arquivos in os.walk(_NOTES):
        dirs[:] = [d for d in dirs if not d.startswith("_archive")]
        for nome in arquivos:
            if nome.lower().endswith(".md") and not nome.startswith("_"):
                with open(os.path.join(raiz, nome), encoding="utf-8", errors="ignore") as fh:
                    textos.append(fh.read())
    nomes = set()
    for caminho in dict.fromkeys((_EXTRAS, _extras_do_banco(banco))):
        if not os.path.isfile(caminho):
            continue
        with open(caminho, encoding="utf-8") as fh:
            for linha in fh:
                nome = linha.split("#", 1)[0].strip()
                if nome:
                    nomes.add(nome.upper())
    nomes.update(a.strip().upper() for a in argv_extras if a.strip())

    for texto in textos:
        limpo = _RE_COMENTARIO_SQL.sub(" ", texto)
        nomes.update(m.upper() for m in _RE_FROM_JOIN.findall(limpo))
        for m in _RE_FM_TABELAS.finditer(texto):
            nomes.update(x.strip().strip("\"'").upper() for x in m.group(1).split(",") if x.strip())
        for m in _RE_FM_PONTES.finditer(texto):
            nomes.update((m.group(1).upper(), m.group(2).upper()))
    nomes = {n for n in nomes if len(n) >= 2 and not n.startswith(("RDB$", "MON$"))}
    return sorted(nomes - _KEYWORDS_SQL)


@contextmanager
def _cursor(conn):
    """Cursor como context manager em qualquer driver (o do fdb e o do sqlite3
    não implementam o protocolo)."""
    cur = conn.cursor()
    try:
        yield cur
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _texto_blob(v):
    """Firebird devolve rdb$description como BLOB; normaliza para texto."""
    if v is None:
        return None
    try:
        if hasattr(v, "read"):
            v = v.read()
        if isinstance(v, bytes):
            return v.decode("utf-8", "replace").strip() or None
        return str(v).strip() or None
    except Exception:
        return None


def _todas_do_schema(conn, engine):
    """Todas as tabelas e views do schema do perfil (para ``--todas``)."""
    with _cursor(conn) as cur:
        if engine == "mysql":
            cur.execute("SELECT TABLE_NAME FROM information_schema.tables "
                        "WHERE TABLE_SCHEMA = DATABASE()")
        elif engine == "sqlserver":
            cur.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES")
        elif engine == "postgres":
            cur.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema()")
        elif engine == "firebird":
            cur.execute("SELECT TRIM(rdb$relation_name) FROM rdb$relations "
                        "WHERE rdb$system_flag = 0 OR rdb$system_flag IS NULL")
        elif engine == "sqlite":
            cur.execute("SELECT name FROM sqlite_master "
                        "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'")
        else:
            cur.execute("SELECT object_name FROM all_objects "
                        "WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') "
                        "AND object_type IN ('TABLE', 'VIEW')")
        return sorted({r[0].upper() for r in cur.fetchall() if r[0]})


def _views_do_banco(conn, engine, teto=30):
    """Nomes das views do banco — entram no catálogo de graça.

    Views costumam ser exatamente o que o modelo deve preferir a reinventar o
    join: alguém já resolveu as regras ali. Acima do teto, desiste (banco com view
    demais não cabe; aí o caminho é ``tabelas_extras``).
    """
    try:
        with _cursor(conn) as cur:
            if engine == "mysql":
                cur.execute("SELECT TABLE_NAME FROM information_schema.views "
                            "WHERE TABLE_SCHEMA = DATABASE()")
            elif engine == "sqlserver":
                cur.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.VIEWS")
            elif engine == "postgres":
                cur.execute("SELECT table_name FROM information_schema.views "
                            "WHERE table_schema = current_schema()")
            elif engine == "firebird":
                cur.execute("SELECT TRIM(rdb$relation_name) FROM rdb$relations "
                            "WHERE rdb$view_blr IS NOT NULL "
                            "AND (rdb$system_flag = 0 OR rdb$system_flag IS NULL)")
            elif engine == "sqlite":
                cur.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
            else:
                return set()
            # Sem .upper(): em banco case-sensitive o caixa É o nome. Os
            # descritores comparam com UPPER() dos dois lados.
            nomes = {r[0] for r in cur.fetchall() if r[0]}
        return nomes if len(nomes) <= teto else set()
    except Exception as e:
        print(f"(views não listadas: {e})", file=sys.stderr)
        return set()


def _descrever_sqlserver(conn, nomes):
    """{tabela: (tipo, comentário, [(coluna, tipo, nulo, comentário)])} no SQL Server.

    Em lote (um IN com todos os candidatos), e não uma query por tabela: em banco
    de milhares de tabelas o custo de ida e volta apareceria.
    """
    if not nomes:
        return {}
    marcadores = ", ".join(["%s"] * len(nomes))
    params = tuple(n.upper() for n in nomes)
    resultado, tipos, comentarios = {}, {}, {}
    with _cursor(conn) as cur:
        cur.execute(
            f"SELECT TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
            f"WHERE UPPER(TABLE_NAME) IN ({marcadores})", params)
        for tab, tipo in cur.fetchall():
            tipos[tab.upper()] = "VIEW" if tipo == "VIEW" else "TABLE"

        # Descrição de tabela/coluna via extended properties: muitas vezes vazio,
        # mas é de graça — best-effort, nunca derruba a geração.
        try:
            cur.execute(
                f"""SELECT o.name, ISNULL(c.name, ''), CAST(ep.value AS NVARCHAR(400))
                      FROM sys.extended_properties ep
                      JOIN sys.objects o ON o.object_id = ep.major_id
                      LEFT JOIN sys.columns c
                             ON c.object_id = ep.major_id AND c.column_id = ep.minor_id
                     WHERE ep.name = 'MS_Description'
                       AND UPPER(o.name) IN ({marcadores})""", params)
            for tab, col, desc in cur.fetchall():
                comentarios[(tab.upper(), (col or "").upper())] = desc
        except Exception:
            pass

        cur.execute(
            f"""SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
                       NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE UPPER(TABLE_NAME) IN ({marcadores})
                 ORDER BY TABLE_NAME, ORDINAL_POSITION""", params)
        for tab, col, dtype, tam, prec, escala, nulo in cur.fetchall():
            up = tab.upper()
            if tam not in (None, 0):
                dtype = f"{dtype}({'MAX' if tam == -1 else tam})"
            elif prec:
                dtype = f"{dtype}({prec},{escala or 0})"
            resultado.setdefault(up, []).append(
                (col, dtype, "Y" if nulo == "YES" else "N",
                 comentarios.get((up, col.upper())))
            )
    return {t: (tipos.get(t, "TABLE"), comentarios.get((t, "")), cols)
            for t, cols in resultado.items()}


# Identificador do Firebird cabe em 31 caracteres. Comparar rdb$relation_name com
# um parâmetro mais longo NÃO devolve "não achei": o Firebird estoura com
# "string right truncation" no fetch — e derrubaria a geração inteira.
_FB_MAX_IDENT = 31


def _descrever_firebird(conn, nomes, lote=150):
    """Idem, no Firebird (rdb$ metadata). Em lote, e sem nome longo demais."""
    nomes = [n for n in nomes if len(n) <= _FB_MAX_IDENT]
    if not nomes:
        return {}
    resultado, meta = {}, {}
    for i in range(0, len(nomes), lote):
        _descrever_firebird_lote(conn, nomes[i:i + lote], resultado, meta)
    return {t: (meta.get(t, ("TABLE", None))[0], meta.get(t, ("TABLE", None))[1], cols)
            for t, cols in resultado.items()}


def _descrever_firebird_lote(conn, nomes, resultado, meta):
    marcadores = ", ".join(["?"] * len(nomes))
    params = [n.upper() for n in nomes]
    with _cursor(conn) as cur:
        cur.execute(
            f"""SELECT TRIM(rdb$relation_name),
                       TRIM(CASE WHEN rdb$view_blr IS NULL THEN 'TABLE' ELSE 'VIEW' END),
                       rdb$description
                  FROM rdb$relations
                 WHERE UPPER(TRIM(rdb$relation_name)) IN ({marcadores})""", params)
        for tab, tipo, desc in cur.fetchall():
            meta[tab.upper()] = (tipo, _texto_blob(desc))

        cur.execute(
            f"""SELECT TRIM(rf.rdb$relation_name), TRIM(rf.rdb$field_name),
                       TRIM(t.rdb$type_name), f.rdb$field_length,
                       f.rdb$field_precision, f.rdb$field_scale,
                       CASE WHEN rf.rdb$null_flag = 1 THEN 'N' ELSE 'Y' END,
                       rf.rdb$description
                  FROM rdb$relation_fields rf
                  JOIN rdb$fields f ON f.rdb$field_name = rf.rdb$field_source
                  LEFT JOIN rdb$types t
                         ON t.rdb$type = f.rdb$field_type
                        AND t.rdb$field_name = 'RDB$FIELD_TYPE'
                 WHERE UPPER(TRIM(rf.rdb$relation_name)) IN ({marcadores})
                 ORDER BY rf.rdb$relation_name, rf.rdb$field_position""", params)
        for tab, col, dtype, tam, prec, escala, nulo, desc in cur.fetchall():
            dtype = (dtype or "?").title()
            if prec:  # NUMERIC/DECIMAL: escala vem negativa no rdb$
                dtype = f"NUMERIC({prec},{abs(escala or 0)})"
            elif dtype.upper() in ("TEXT", "VARYING") and tam:
                dtype = f"{'CHAR' if dtype.upper() == 'TEXT' else 'VARCHAR'}({tam})"
            resultado.setdefault(tab.upper(), []).append(
                (col, dtype.upper(), nulo, _texto_blob(desc)))


def _descrever_mysql(conn, nomes):
    """Idem, no MySQL/MariaDB (information_schema). Em lote.

    COLUMN_TYPE (e não DATA_TYPE) já vem com tamanho/precisão — ``varchar(255)``,
    ``tinyint(1)`` —, o que evita remontar o tipo na mão.

    A chave do resultado é o nome COMO ESTÁ NO BANCO, sem .upper(): com
    ``lower_case_table_names=0`` o servidor é case-sensitive. O casamento com os
    candidatos continua sem caixa (UPPER dos dois lados); o caixa só decide o que
    vai escrito no dicionário.
    """
    if not nomes:
        return {}
    marcadores = ", ".join(["%s"] * len(nomes))
    params = tuple(n.upper() for n in nomes)
    resultado, meta = {}, {}
    with _cursor(conn) as cur:
        cur.execute(
            f"""SELECT TABLE_NAME, TABLE_TYPE, TABLE_COMMENT
                  FROM information_schema.tables
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND UPPER(TABLE_NAME) IN ({marcadores})""", params)
        for tab, tipo, com in cur.fetchall():
            com = (com or "").strip()
            # O MySQL põe TABLE_COMMENT = "VIEW" em toda view: é ruído, não comentário.
            meta[tab] = ("VIEW" if tipo == "VIEW" else "TABLE",
                         com if com and com.upper() != "VIEW" else None)
        cur.execute(
            f"""SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT
                  FROM information_schema.columns
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND UPPER(TABLE_NAME) IN ({marcadores})
                 ORDER BY TABLE_NAME, ORDINAL_POSITION""", params)
        for tab, col, dtype, nulo, com in cur.fetchall():
            resultado.setdefault(tab, []).append(
                (col, dtype, "Y" if nulo == "YES" else "N", (com or "").strip() or None))
    return {t: (meta.get(t, ("TABLE", None))[0], meta.get(t, ("TABLE", None))[1], cols)
            for t, cols in resultado.items()}


def _descrever_postgres(conn, nomes):
    """Idem, no PostgreSQL. Em lote, direto no catálogo (pg_class/pg_attribute).

    ``format_type()`` já devolve o tipo com tamanho/precisão pronto
    (``character varying(255)``, ``numeric(10,2)``) — remontar isso a partir do
    information_schema erra nos tipos que não têm nenhum dos dois.
    ``obj_description``/``col_description`` são onde ficam os COMMENT ON.
    """
    if not nomes:
        return {}
    marcadores = ", ".join(["%s"] * len(nomes))
    params = tuple(n.upper() for n in nomes)
    # 'r' tabela, 'v' view, 'm' view materializada, 'p' particionada, 'f' externa.
    relkinds = "('r','v','m','p','f')"
    resultado, meta = {}, {}
    with _cursor(conn) as cur:
        cur.execute(
            f"""SELECT c.relname,
                       CASE WHEN c.relkind IN ('v','m') THEN 'VIEW' ELSE 'TABLE' END,
                       obj_description(c.oid, 'pg_class')
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = current_schema()
                   AND c.relkind IN {relkinds}
                   AND UPPER(c.relname) IN ({marcadores})""", params)
        for tab, tipo, com in cur.fetchall():
            meta[tab] = (tipo, (com or "").strip() or None)
        cur.execute(
            f"""SELECT c.relname, a.attname,
                       format_type(a.atttypid, a.atttypmod),
                       CASE WHEN a.attnotnull THEN 'N' ELSE 'Y' END,
                       col_description(a.attrelid, a.attnum)
                  FROM pg_attribute a
                  JOIN pg_class c ON c.oid = a.attrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = current_schema()
                   AND c.relkind IN {relkinds}
                   AND a.attnum > 0 AND NOT a.attisdropped
                   AND UPPER(c.relname) IN ({marcadores})
                 ORDER BY c.relname, a.attnum""", params)
        for tab, col, dtype, nulo, com in cur.fetchall():
            resultado.setdefault(tab, []).append(
                (col, dtype, nulo, (com or "").strip() or None))
    return {t: (meta.get(t, ("TABLE", None))[0], meta.get(t, ("TABLE", None))[1], cols)
            for t, cols in resultado.items()}


_RE_COMENTARIO_COLUNA = re.compile(r'^\s*["`\[]?([A-Za-z_][A-Za-z0-9_]*)["`\]]?\s.*?--\s*(.+?)\s*$')


def _comentarios_do_create(sql):
    """{COLUNA: comentário} lido dos ``-- ...`` no fim das linhas do CREATE TABLE.

    O SQLite não tem COMMENT ON, mas guarda o texto original do CREATE em
    ``sqlite_master.sql`` — com os comentários de linha. É o único lugar onde quem
    criou o schema pôde explicar uma coluna.
    """
    saida = {}
    for linha in (sql or "").splitlines():
        m = _RE_COMENTARIO_COLUNA.match(linha)
        if m and m.group(1).upper() not in ("CREATE", "CONSTRAINT", "PRIMARY", "FOREIGN"):
            saida[m.group(1).upper()] = m.group(2)
    return saida


def _descrever_sqlite(conn, nomes):
    """Idem, no SQLite (sqlite_master + pragma_table_info).

    Comentário de coluna vem dos ``-- ...`` do CREATE TABLE original (ver
    ``_comentarios_do_create``); comentário de TABELA o SQLite não guarda — o que
    faltar vai no ``contexto/cabecalho_<banco>.md`` e nas NOTES. A chave primária
    entra no comentário da coluna: é o que o modelo mais usa para montar o join.
    """
    if not nomes:
        return {}
    marcadores = ", ".join(["?"] * len(nomes))
    resultado = {}
    with _cursor(conn) as cur:
        cur.execute(
            f"""SELECT name, type, sql FROM sqlite_master
                 WHERE type IN ('table', 'view')
                   AND UPPER(name) IN ({marcadores})""",
            tuple(n.upper() for n in nomes))
        objetos = cur.fetchall()
        for nome, tipo, sql in objetos:
            notas = _comentarios_do_create(sql) if tipo == "table" else {}
            cur.execute(
                'SELECT name, type, "notnull", pk FROM pragma_table_info(?) ORDER BY cid',
                (nome,))
            colunas = []
            for col, dtype, notnull, pk in cur.fetchall():
                com = "; ".join(x for x in ("chave primária" if pk else "",
                                            notas.get(col.upper(), "")) if x)
                colunas.append((col, (dtype or "").upper() or "?",
                                "N" if notnull else "Y", com or None))
            resultado[nome] = ("VIEW" if tipo == "view" else "TABLE", None, colunas)
    return resultado


def _descrever_oracle(conn, nomes):
    """Devolve {tabela: (tipo, comentário, [(coluna, tipo, nulo, comentário)])}.

    Uma única conexão para tudo; tabelas inexistentes são simplesmente descartadas
    (o pool de candidatos gera falsos positivos por natureza).
    """
    resultado = {}
    with conn.cursor() as cur:
        for tab in nomes:
            cur.execute(
                """
                SELECT c.column_name,
                       c.data_type
                       || CASE WHEN c.data_type = 'NUMBER' AND c.data_precision IS NOT NULL
                               THEN '(' || c.data_precision
                                    || CASE WHEN c.data_scale > 0 THEN ',' || c.data_scale END || ')'
                               WHEN c.data_type LIKE 'VARCHAR%' THEN '(' || c.data_length || ')'
                               ELSE '' END,
                       c.nullable,
                       cc.comments
                  FROM all_tab_columns c
                  LEFT JOIN all_col_comments cc
                    ON cc.owner = c.owner AND cc.table_name = c.table_name
                   AND cc.column_name = c.column_name
                 WHERE c.table_name = :tab
                 ORDER BY c.column_id
                """,
                {"tab": tab},
            )
            colunas = cur.fetchall()
            if not colunas:
                continue  # não existe (falso positivo)

            cur.execute(
                """
                SELECT o.object_type, tc.comments
                  FROM all_objects o
                  LEFT JOIN all_tab_comments tc
                    ON tc.owner = o.owner AND tc.table_name = o.object_name
                 WHERE o.object_name = :tab
                   AND o.object_type IN ('TABLE', 'VIEW')
                   AND ROWNUM = 1
                """,
                {"tab": tab},
            )
            meta = cur.fetchone() or ("TABLE", None)
            resultado[tab] = (meta[0], meta[1], colunas)
    return resultado


_DESCRITORES = {
    "mysql": _descrever_mysql,
    "postgres": _descrever_postgres,
    "sqlserver": _descrever_sqlserver,
    "firebird": _descrever_firebird,
    "sqlite": _descrever_sqlite,
    "oracle": _descrever_oracle,
}


def _descrever_todas(nomes, perfil, todas=False, limite=500):
    """Descreve os candidatos no banco do perfil. O que não existe é descartado."""
    engine = engine_do_perfil(perfil)
    conn = create_readonly_connection(perfil)
    try:
        if todas:
            catalogo = _todas_do_schema(conn, engine)
            if len(catalogo) > limite:
                raise SystemExit(
                    f"--todas: o schema tem {len(catalogo)} objetos (limite {limite}). "
                    "Num banco desse tamanho, catalogue pelo uso (NOTES + "
                    "tabelas_extras) ou aumente --limite conscientemente.")
            nomes = sorted(set(nomes) | set(catalogo))
        elif engine != "oracle":
            # Teto de views maior no MySQL: lá é comum o schema inteiro ser de
            # views (banco de fornecedor que expõe só views ao cliente).
            nomes = sorted(set(nomes) | _views_do_banco(
                conn, engine, teto=60 if engine == "mysql" else 30))
        return _DESCRITORES[engine](conn, nomes)
    finally:
        conn.close()


def _cabecalho_manual(banco):
    caminho = os.path.join(_CONTEXTO, f"cabecalho_{banco}.md")
    if not os.path.isfile(caminho):
        return ""
    with open(caminho, encoding="utf-8") as fh:
        return fh.read().strip()


def _escrever(descricoes, perfil):
    engine = engine_do_perfil(perfil)
    banco = _perfis.banco(perfil)
    saida = os.path.join(_CONTEXTO, f"dicionario_{banco}.md")
    titulo = _perfis.descricao(perfil) or banco
    leitores = " ou ".join(f"`--perfil {p}`"
                           for p in (_perfis.perfis_do_banco(banco) or (perfil,)))
    os.makedirs(_CONTEXTO, exist_ok=True)
    partes = [
        f"# Dicionário de dados — {titulo}\n",
        "Gerado por gerar_dicionario.py a partir das tabelas citadas em NOTES/ e dos extras.\n",
        f"Dialeto: {_perfis.dialeto(engine)}.\n",
        f"Consulte com {leitores}.\n",
    ]
    manual = _cabecalho_manual(banco)
    if manual:
        partes.append(manual + "\n")

    partes.append("\n## Schema das tabelas e views conhecidas\n")
    for tab in sorted(descricoes, key=str.upper):
        tipo, comentario, colunas = descricoes[tab]
        cab = f"### {tab} ({tipo}"
        cab += f") — {comentario}" if comentario else ")"
        partes.append(cab + "\n")
        partes.append("| Coluna | Tipo | Nulo | Comentário |")
        partes.append("|---|---|---|---|")
        for nome, dtype, nulo, com in colunas:
            com = (com or "").replace("|", "/").replace("\n", " ")
            partes.append(f"| {nome} | {dtype} | {nulo} | {com} |")
        partes.append("")

    # As NOTES não são copiadas para cá: o recuperar.py lê de NOTES/, e duplicar
    # só criaria uma segunda cópia para divergir.
    partes.append("\n## Conhecimento acumulado (NOTES)\n")
    partes.append(f"As regras e armadilhas deste banco ficam em NOTES/{banco}/.\n")

    with open(saida, "w", encoding="utf-8") as fh:
        fh.write("\n".join(partes))
    return saida


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(description="Gera o dicionário de dados de um perfil.")
    p.add_argument("--perfil", default=None,
                   help=f"banco a descrever (padrão: {_perfis.PERFIL_PADRAO or '—'})")
    p.add_argument("--todas", action="store_true",
                   help="cataloga todas as tabelas e views do schema")
    p.add_argument("--limite", type=int, default=500,
                   help="teto de objetos aceito por --todas (padrão 500)")
    p.add_argument("tabelas", nargs="*", help="tabelas avulsas a incluir")
    args = p.parse_args(argv)

    try:
        perfil = _perfis.conferir(args.perfil)
    except ValueError as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 2
    banco = _perfis.banco(perfil)

    nomes = _candidatos(args.tabelas, banco)
    origem = "o schema inteiro" if args.todas else f"{len(nomes)} candidatos"
    print(f"Descrevendo {origem} em {perfil} ({engine_do_perfil(perfil)})...")
    descricoes = _descrever_todas(nomes, perfil, todas=args.todas, limite=args.limite)
    caminho = _escrever(descricoes, perfil)
    tamanho = os.path.getsize(caminho)
    print(f"OK — {len(descricoes)} tabelas/views descritas em {caminho}")
    print(f"Tamanho: {tamanho / 1024:.0f} KB (~{tamanho // 4} tokens estimados)")
    # Comparação sem caixa: o descritor devolve o nome como está no banco, e o
    # candidato pode ter vindo em outro caixa da nota/extras.
    descritas = {d.upper() for d in descricoes}
    descartados = sorted(n for n in set(nomes) if n.upper() not in descritas)
    if descartados:
        print(f"Ignorados (não existem neste banco): {len(descartados)} nomes")

    # Índices derivados — grafo e vetorial leem TODOS os dicionários, então a
    # regeneração vale para o conjunto. Best-effort: nenhum dos dois derruba o
    # dicionário recém-escrito.
    try:
        grafo.construir()
    except Exception as exc:
        print(f"(grafo não gerado: {exc})")
    try:
        vetorial.construir()
    except Exception as exc:
        print(f"(índice vetorial não gerado: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
