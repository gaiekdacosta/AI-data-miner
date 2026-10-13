#!/usr/bin/env python3
"""CLI de investigação SOMENTE LEITURA nos bancos configurados.

Pensada para o Claude (e para quem mais quiser) rodar consultas, descobrir o
schema e investigar dados sem risco de alterar o banco: todo comando que não
comece com SELECT ou WITH é recusado antes de ir ao banco.

O banco é escolhido pelo perfil (``--perfil``), declarado no ``.env`` — ver
``perfis.py`` e ``.env.example``.

Uso (a partir da raiz do projeto):

    python3 consultar.py ping
    python3 consultar.py query "SELECT * FROM pedidos LIMIT 5"
    python3 consultar.py --perfil rh query "SELECT TOP 5 * FROM funcionarios"
    python3 consultar.py query -f minha_consulta.sql
    python3 consultar.py tabelas "PED%"          # filtro LIKE, sem caixa
    python3 consultar.py describe pedidos
    python3 consultar.py amostra pedidos --limite 20
    python3 consultar.py perfis                  # o que está configurado

Opções globais:
    --perfil P       qual banco (padrão: PERFIL_PADRAO do .env)
    --csv            saída em CSV (em vez de tabela alinhada)
    --max-linhas N   limite de linhas exibidas (padrão 200; 0 = sem limite)
"""

import argparse
import csv
import io
import sys
import threading

try:  # como pacote
    from .conexao import (
        create_readonly_connection, usuario_conexao, credenciais_ok,
        engine_do_perfil, teto_de_tempo_s, PERFIL_PADRAO,
    )
    from . import perfis as _perfis
except ImportError:  # como script
    from conexao import (
        create_readonly_connection, usuario_conexao, credenciais_ok,
        engine_do_perfil, teto_de_tempo_s, PERFIL_PADRAO,
    )
    import perfis as _perfis

# Apenas comandos de leitura são permitidos.
_COMANDOS_LEITURA = ("SELECT", "WITH")
# Palavras que jamais devem aparecer numa consulta de investigação.
_PROIBIDO = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "ALTER", "CREATE",
    "TRUNCATE", "GRANT", "REVOKE", "COMMIT", "ROLLBACK", "EXECUTE",
    "BEGIN", "CALL",
)


def _validar_select(sql):
    """Garante que o SQL é uma única instrução de leitura. Levanta ValueError."""
    limpo = (sql or "").strip().rstrip(";").strip()
    if not limpo:
        raise ValueError("Consulta vazia.")
    if ";" in limpo:
        raise ValueError(
            "Múltiplas instruções não são permitidas (remova o ';' do meio)."
        )
    primeira = limpo.split(None, 1)[0].upper()
    if primeira not in _COMANDOS_LEITURA:
        raise ValueError(
            f"Apenas consultas de leitura são permitidas (SELECT/WITH). "
            f"Comando recebido: '{primeira}'."
        )
    # Por palavra inteira, para não barrar nomes de coluna como CREATED_AT.
    tokens = {t.strip("(),").upper() for t in limpo.replace("\n", " ").split()}
    bloqueado = tokens.intersection(_PROIBIDO)
    if bloqueado:
        raise ValueError(
            f"A consulta contém comando(s) não permitido(s): {', '.join(sorted(bloqueado))}."
        )
    return limpo


def _cancelar_firebird(conn):
    """Interrompe a operação em curso numa conexão Firebird.

    O ``fdb`` 2.x não expõe ``cancel_operation`` na Connection nem prototipa
    ``fb_cancel_operation`` na API — mas o símbolo ESTÁ na ``libfbclient`` que ele
    já carregou, e o handle da conexão está em ``conn._db_handle``. Ligamos por
    ctypes na biblioteca JÁ carregada (um ``CDLL`` novo carregaria uma segunda
    cópia do cliente no processo).

    ``fb_cancel_raise`` faz a chamada pendurada retornar com erro, que é o que se
    quer: quem está no ``fetch`` recebe exceção em vez de esperar para sempre.
    """
    import ctypes

    import fdb
    from fdb import ibase

    api = fdb.load_api()
    status = (ibase.ISC_STATUS * 20)()
    api.client_library.fb_cancel_operation(
        status, ctypes.byref(conn._db_handle), ibase.fb_cancel_raise
    )


# Como interromper uma consulta pendurada, por engine. Ausente = o driver já tem
# teto próprio (mysql: read_timeout + MAX_EXECUTION_TIME; postgres:
# statement_timeout; sqlserver: timeout do pymssql) e dispensa o vigia.
_CANCELADORES = {
    "firebird": _cancelar_firebird,
    "oracle": lambda conn: conn.cancel(),
    "sqlite": lambda conn: conn.interrupt(),
}


class _Watchdog:
    """Cancela a consulta se ela passar do teto de tempo do perfil.

    POR QUE EXISTE: uma consulta que nunca volta (rede que derruba o pacote sem
    avisar, fetch pendurado no driver) prende a thread e a conexão até o banco
    resolver responder — o que pode ser nunca. No servidor MCP isso vira uma
    thread vazada por travamento; sem o corte, elas se acumulam.

    O vigia cobre execute E fetch: o travamento típico é no fetch, então cercar só
    a execução deixaria passar exatamente o caso que o motivou.

    Desligar: ``db_<perfil>_query_timeout_s=0`` (ou ``db_query_timeout_s=0``
    para todos).
    """

    def __init__(self, conn, engine, teto_s):
        self._cancelar = _CANCELADORES.get(engine)
        self._teto = teto_s
        self._timer = None
        self.disparou = False

        if self._cancelar and teto_s:
            self._timer = threading.Timer(teto_s, self._disparar, args=(conn,))
            self._timer.daemon = True

    def __enter__(self):
        if self._timer:
            self._timer.start()
        return self

    def _disparar(self, conn):
        # A ordem importa: marcar ANTES de cancelar, senão a exceção do cancel
        # pode chegar ao `except` antes de `disparou` virar True, e o erro sairia
        # como falha do banco em vez de estouro de tempo.
        self.disparou = True
        try:
            self._cancelar(conn)
        except Exception:
            pass  # cancelar é best-effort; o erro que interessa é o do teto

    def __exit__(self, *_exc):
        if self._timer:
            self._timer.cancel()
        return False


def _executar(sql, params=None, max_linhas=200, perfil=None):
    """Executa um SELECT e devolve (colunas, linhas, truncado).

    Levanta ``TimeoutError`` se a consulta passar do teto do perfil (ver
    ``conexao.teto_de_tempo_s``).
    """
    conn = create_readonly_connection(perfil)
    engine = engine_do_perfil(perfil)
    teto = teto_de_tempo_s(perfil)
    try:
        # O cursor do fdb (Firebird) não implementa o protocolo de context
        # manager, então o cursor é gerenciado à mão — funciona em todos.
        cursor = conn.cursor()
        try:
            with _Watchdog(conn, engine, teto) as cao:
                try:
                    # Só passa params quando existem: o pymssql trata '%' como
                    # marcador e quebraria em SQL cru (LIKE '%x%') se recebesse
                    # um dict vazio. Sem params, a query segue literal.
                    if params:
                        cursor.execute(sql, params)
                    else:
                        cursor.execute(sql)
                    colunas = [d[0] for d in cursor.description]
                    linhas = []
                    truncado = False
                    for i, row in enumerate(cursor):
                        if max_linhas and i >= max_linhas:
                            truncado = True
                            break
                        linhas.append(row)
                except Exception:
                    if cao.disparou:
                        raise TimeoutError(
                            f"a consulta passou do teto de {teto}s no perfil "
                            f"'{perfil or PERFIL_PADRAO}' e foi cancelada. Reduza o "
                            f"período, filtre mais cedo ou peça um teto maior "
                            f"para este perfil."
                        ) from None
                    raise
        finally:
            cursor.close()
    finally:
        conn.close()
    return colunas, linhas, truncado


def _formatar_valor(v):
    if v is None:
        return "NULL"
    return str(v)


def _imprimir(colunas, linhas, como_csv=False, truncado=False, max_linhas=200):
    if como_csv:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(colunas)
        for row in linhas:
            w.writerow(["" if v is None else v for v in row])
        sys.stdout.write(buf.getvalue())
    else:
        celulas = [[_formatar_valor(v) for v in row] for row in linhas]
        larguras = [len(c) for c in colunas]
        for row in celulas:
            for i, c in enumerate(row):
                larguras[i] = max(larguras[i], len(c))
        sep = " | "
        print(sep.join(c.ljust(larguras[i]) for i, c in enumerate(colunas)))
        print("-+-".join("-" * w for w in larguras))
        for row in celulas:
            print(sep.join(c.ljust(larguras[i]) for i, c in enumerate(row)))

    print(f"\n({len(linhas)} linha(s))", end="")
    if truncado:
        print(f" — resultado truncado em {max_linhas} linhas; use --max-linhas 0 para tudo.", end="")
    print()


# ----------------------------- subcomandos --------------------------------- #

def cmd_query(args):
    if args.arquivo:
        with open(args.arquivo, "r", encoding="utf-8") as fh:
            sql = fh.read()
    else:
        sql = args.sql
    if not sql:
        print("Informe a SQL como argumento ou via -f arquivo.sql", file=sys.stderr)
        return 2
    sql = _validar_select(sql)
    colunas, linhas, truncado = _executar(sql, max_linhas=args.max_linhas, perfil=args.perfil)
    _imprimir(colunas, linhas, args.csv, truncado, args.max_linhas)
    return 0


def cmd_tabelas(args):
    filtro = args.filtro or "%"
    engine = engine_do_perfil(args.perfil)
    if engine == "mysql":
        # TABLE_SCHEMA = DATABASE() é obrigatório: sem ele entram os objetos de
        # information_schema/performance_schema no meio dos do banco.
        sql = """
            SELECT TABLE_NAME, TABLE_TYPE
              FROM information_schema.tables
             WHERE TABLE_SCHEMA = DATABASE()
               AND UPPER(TABLE_NAME) LIKE UPPER(%s)
             ORDER BY TABLE_NAME
        """
        params = (filtro,)
    elif engine == "sqlserver":
        sql = """
            SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
              FROM INFORMATION_SCHEMA.TABLES
             WHERE UPPER(TABLE_NAME) LIKE UPPER(%(filtro)s)
             ORDER BY TABLE_SCHEMA, TABLE_NAME
        """
        params = {"filtro": filtro}
    elif engine == "postgres":
        # current_schema() é o 1º do search_path, fixado na conexão.
        sql = """
            SELECT table_name, table_type
              FROM information_schema.tables
             WHERE table_schema = current_schema()
               AND UPPER(table_name) LIKE UPPER(%s)
             ORDER BY table_name
        """
        params = (filtro,)
    elif engine == "firebird":
        # Catálogo em rdb$relations; ? é o marcador posicional (qmark).
        sql = """
            SELECT TRIM(rdb$relation_name) AS TABLE_NAME
              FROM rdb$relations
             WHERE rdb$view_blr IS NULL
               AND (rdb$system_flag = 0 OR rdb$system_flag IS NULL)
               AND UPPER(rdb$relation_name) LIKE UPPER(?)
             ORDER BY rdb$relation_name
        """
        params = [filtro]
    elif engine == "sqlite":
        sql = """
            SELECT name AS table_name, type AS table_type
              FROM sqlite_master
             WHERE type IN ('table', 'view')
               AND name NOT LIKE 'sqlite_%'
               AND UPPER(name) LIKE UPPER(?)
             ORDER BY name
        """
        params = (filtro,)
    else:
        sql = """
            SELECT table_name
              FROM all_tables
             WHERE UPPER(table_name) LIKE UPPER(:filtro)
             ORDER BY table_name
        """
        params = {"filtro": filtro}
    colunas, linhas, truncado = _executar(sql, params, args.max_linhas, perfil=args.perfil)
    _imprimir(colunas, linhas, args.csv, truncado, args.max_linhas)
    return 0


def cmd_describe(args):
    engine = engine_do_perfil(args.perfil)
    if engine == "mysql":
        sql = """
            SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT
              FROM information_schema.columns
             WHERE TABLE_SCHEMA = DATABASE()
               AND UPPER(TABLE_NAME) = UPPER(%s)
             ORDER BY ORDINAL_POSITION
        """
        params = (args.tabela,)
    elif engine == "sqlserver":
        sql = """
            SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
              FROM INFORMATION_SCHEMA.COLUMNS
             WHERE UPPER(TABLE_NAME) = UPPER(%(tab)s)
             ORDER BY ORDINAL_POSITION
        """
        params = {"tab": args.tabela}
    elif engine == "postgres":
        # format_type() devolve o tipo já com tamanho/precisão (varchar(255),
        # numeric(10,2)); col_description() é onde ficam os COMMENT ON COLUMN.
        sql = """
            SELECT a.attname AS column_name,
                   format_type(a.atttypid, a.atttypmod) AS data_type,
                   CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable,
                   col_description(a.attrelid, a.attnum) AS comentario
              FROM pg_attribute a
              JOIN pg_class c ON c.oid = a.attrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = current_schema()
               AND UPPER(c.relname) = UPPER(%s)
               AND a.attnum > 0 AND NOT a.attisdropped
             ORDER BY a.attnum
        """
        params = (args.tabela,)
    elif engine == "firebird":
        sql = """
            SELECT TRIM(rf.rdb$field_name) AS COLUMN_NAME,
                   TRIM(t.rdb$type_name)   AS DATA_TYPE,
                   f.rdb$field_length      AS LENGTH,
                   CASE WHEN rf.rdb$null_flag = 1 THEN 'NO' ELSE 'YES' END AS IS_NULLABLE
              FROM rdb$relation_fields rf
              JOIN rdb$fields f ON f.rdb$field_name = rf.rdb$field_source
              LEFT JOIN rdb$types t
                     ON t.rdb$type = f.rdb$field_type
                    AND t.rdb$field_name = 'RDB$FIELD_TYPE'
             WHERE UPPER(rf.rdb$relation_name) = UPPER(?)
             ORDER BY rf.rdb$field_position
        """
        params = [args.tabela]
    elif engine == "sqlite":
        # pragma_table_info como função de tabela: aceita parâmetro (o PRAGMA
        # comum, não) e compara o nome sem caixa, como o resto do SQLite.
        sql = """
            SELECT name AS column_name, type AS data_type,
                   CASE WHEN "notnull" THEN 'NO' ELSE 'YES' END AS is_nullable,
                   CASE WHEN pk > 0 THEN 'PK' ELSE '' END AS chave
              FROM pragma_table_info(?)
             ORDER BY cid
        """
        params = (args.tabela,)
    else:
        sql = """
            SELECT column_name, data_type, data_length, nullable
              FROM all_tab_columns
             WHERE UPPER(table_name) = UPPER(:tab)
             ORDER BY column_id
        """
        params = {"tab": args.tabela}
    colunas, linhas, truncado = _executar(sql, params, args.max_linhas, perfil=args.perfil)
    if not linhas:
        print(f"Tabela '{args.tabela}' não encontrada (ou sem permissão de leitura).")
        return 1
    _imprimir(colunas, linhas, args.csv, truncado, args.max_linhas)
    return 0


def cmd_amostra(args):
    # Nome de tabela vem do usuário: valida que é um identificador simples.
    tab = args.tabela.strip()
    if not tab.replace("_", "").replace(".", "").replace("$", "").isalnum():
        print(f"Nome de tabela inválido: '{tab}'", file=sys.stderr)
        return 2
    engine = engine_do_perfil(args.perfil)
    if engine in ("mysql", "postgres", "sqlite"):
        # limite é int (argparse), seguro inline.
        sql = f"SELECT * FROM {tab} LIMIT {int(args.limite)}"
        params = None
    elif engine == "sqlserver":
        sql = f"SELECT TOP (%(lim)s) * FROM {tab}"
        params = {"lim": args.limite}
    elif engine == "firebird":
        sql = f"SELECT FIRST {int(args.limite)} * FROM {tab}"
        params = None
    else:
        sql = f"SELECT * FROM {tab} WHERE ROWNUM <= :lim"
        params = {"lim": args.limite}
    colunas, linhas, truncado = _executar(sql, params, args.max_linhas, perfil=args.perfil)
    _imprimir(colunas, linhas, args.csv, truncado, args.max_linhas)
    return 0


def cmd_ping(args):
    if not credenciais_ok(args.perfil):
        print("Configuração incompleta no .env — conexão não configurada "
              "(rode com `query` para ver qual variável falta).", file=sys.stderr)
        return 1
    # Cada engine tem sua "tabela de 1 linha": Oracle DUAL, Firebird rdb$database;
    # os outros dispensam FROM.
    engine = engine_do_perfil(args.perfil)
    if engine in ("sqlserver", "postgres", "mysql", "sqlite"):
        sql = "SELECT 1"
    elif engine == "firebird":
        sql = "SELECT 1 FROM rdb$database"
    else:
        sql = "SELECT 1 FROM DUAL"
    try:
        _executar(sql, max_linhas=1, perfil=args.perfil)
        print(f"OK — conectado como '{usuario_conexao(args.perfil)}' "
              f"(perfil {args.perfil}, {engine}).")
        return 0
    except Exception as e:
        print(f"Falha ao conectar como '{usuario_conexao(args.perfil)}': {e}", file=sys.stderr)
        return 1


def cmd_perfis(_args):
    if not _perfis.PERFIS:
        print("Nenhum perfil configurado: defina PERFIS no .env (veja .env.example).")
        return 1
    for r in _perfis.resumo():
        marcas = []
        if r["perfil"] == PERFIL_PADRAO:
            marcas.append("padrão")
        if r["sensivel"]:
            marcas.append("sensível")
        if r["banco"] != r["perfil"]:
            marcas.append(f"base de conhecimento: {r['banco']}")
        extra = f"  [{', '.join(marcas)}]" if marcas else ""
        print(f"{r['perfil']:<16} {r['engine']:<10} {r['descricao']}{extra}")
    return 0


# Defaults das opções comuns. Aplicados uma única vez em main(): como essas
# opções aparecem tanto no parser principal quanto nos subparsers (parents), um
# default aqui faria o subparser sobrescrever, com o seu próprio default, o valor
# lido antes do subcomando ("--perfil rh ping" viraria o perfil padrão). Por isso
# os argumentos usam default=SUPPRESS e o default real fica aqui.
_DEFAULTS_COMUNS = {"csv": False, "max_linhas": 200, "perfil": PERFIL_PADRAO}


def build_parser():
    comum = argparse.ArgumentParser(add_help=False)
    comum.add_argument("--csv", action="store_true", default=argparse.SUPPRESS,
                       help="saída em CSV")
    comum.add_argument("--max-linhas", type=int, default=argparse.SUPPRESS,
                       help="máximo de linhas exibidas (padrão 200; 0 = sem limite)")
    configurados = ", ".join(_perfis.PERFIS) or "nenhum — defina PERFIS no .env"
    comum.add_argument("--perfil", default=argparse.SUPPRESS,
                       help=f"qual banco consultar (configurados: {configurados}; "
                            f"padrão: {PERFIL_PADRAO or '—'})")

    p = argparse.ArgumentParser(
        prog="consultar.py",
        description="Investigação SOMENTE LEITURA nos bancos configurados no .env. "
                    "Escolha o banco com --perfil.",
        parents=[comum],
    )
    sub = p.add_subparsers(dest="comando", required=True)

    q = sub.add_parser("query", help="executa um SELECT/WITH", parents=[comum])
    q.add_argument("sql", nargs="?", help="a consulta SQL")
    q.add_argument("-f", "--arquivo", help="lê a SQL de um arquivo")
    q.set_defaults(func=cmd_query)

    t = sub.add_parser("tabelas", help="lista tabelas (filtro LIKE opcional)", parents=[comum])
    t.add_argument("filtro", nargs="?", help="ex.: PED%%")
    t.set_defaults(func=cmd_tabelas)

    d = sub.add_parser("describe", help="mostra as colunas de uma tabela", parents=[comum])
    d.add_argument("tabela")
    d.set_defaults(func=cmd_describe)

    a = sub.add_parser("amostra", help="primeiras linhas de uma tabela", parents=[comum])
    a.add_argument("tabela")
    a.add_argument("--limite", type=int, default=10, help="qtde de linhas (padrão 10)")
    a.set_defaults(func=cmd_amostra)

    pi = sub.add_parser("ping", help="testa a conexão", parents=[comum])
    pi.set_defaults(func=cmd_ping)

    pf = sub.add_parser("perfis", help="lista os perfis configurados", parents=[comum])
    pf.set_defaults(func=cmd_perfis)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    for nome, valor in _DEFAULTS_COMUNS.items():
        if not hasattr(args, nome):
            setattr(args, nome, valor)
    try:
        if args.func is not cmd_perfis:
            args.perfil = _perfis.conferir(args.perfil)
        return args.func(args)
    except ValueError as e:
        print(f"Consulta inválida: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"Erro de configuração: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Erro ao consultar o banco: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
