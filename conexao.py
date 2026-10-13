"""Conexão SOMENTE LEITURA com os bancos consultados pela ferramenta.

Cada perfil (ver ``perfis.py``) aponta para um banco, num destes engines:

- **oracle** — ``oracledb``. Modo *thin* por padrão (sem client library); se
  ``ORACLE_CLIENT_LIB_DIR`` aponta para um Oracle Instant Client, entra em modo
  *thick*, que é o que servidor antigo exige (verificador de senha 10g, erro
  ``DPY-3015`` no thin).
- **sqlserver** — ``pymssql`` (wheels com FreeTDS embutido; dispensa ODBC).
- **firebird** — ``fdb`` (exige a ``libfbclient`` no host; a imagem instala).
- **mysql** — ``PyMySQL`` (Python puro; vale para MariaDB).
- **postgres** — ``psycopg2`` (wheel binária, traz a libpq).
- **sqlite** — ``sqlite3`` da biblioteca padrão, aberto em modo ``ro``.

Independentemente do engine, a camada de consulta (``consultar.py``) recusa tudo
que não seja ``SELECT``/``WITH`` — read-only por construção, na aplicação. Onde o
engine permite, a SESSÃO também é posta em read-only (MySQL, PostgreSQL, SQLite),
então o servidor recusa escrita mesmo que algo passasse pela validação.

Variáveis de conexão por perfil (``db_<perfil>_<chave>``):

    engine                       oracle | sqlserver | firebird | mysql | postgres | sqlite
    host, port                   (port tem padrão por engine)
    username, password
    database                     banco (mysql, postgres, sqlserver), alias/caminho do
                                 .fdb (firebird) ou caminho do arquivo (sqlite)
    service_name                 oracle
    schema                       oracle: CURRENT_SCHEMA da sessão; postgres: search_path
    charset                      firebird (UTF8), mysql (utf8mb4)
    ssl, ssl_ca                  mysql — TLS exigido quando ssl=1 (padrão 0)
    sslmode                      postgres — repassado à libpq (disable, require...)
    query_timeout_s              teto da consulta; padrão global db_query_timeout_s=180
    max_execution_ms, select_limit, read_timeout     mysql
    statement_timeout_ms         postgres

Drivers são importados sob demanda: uma instalação só com PostgreSQL não precisa
do resto instalado.
"""

import os
import re
import sys

try:  # como pacote
    from . import perfis as _perfis
except ImportError:  # como script
    import perfis as _perfis

PERFIS = _perfis.PERFIS
PERFIL_PADRAO = _perfis.PERFIL_PADRAO

_AQUI = os.path.dirname(os.path.abspath(__file__))

_PORTAS = {"oracle": "1521", "sqlserver": "1433", "firebird": "3050",
           "mysql": "3306", "postgres": "5432"}

_VERDADE = ("1", "true", "sim", "yes", "on")


def _caminho_sqlite(bruto):
    """Caminho relativo vale a partir da pasta do projeto, não do cwd: assim o
    mesmo .env serve para a CLI local e para o container (onde a pasta é /app)."""
    if not bruto:
        return None
    caminho = os.path.expanduser(bruto)
    if not os.path.isabs(caminho):
        caminho = os.path.join(_AQUI, caminho)
    return os.path.normpath(caminho)


def _resolver_perfil(perfil=None):
    """Configuração de conexão do perfil, como dict.

    Chaves sempre presentes: ``perfil``, ``engine``, ``username``, ``password``,
    ``host``, ``port``, ``database``, ``schema``; as demais dependem do engine.
    """
    p = _perfis.conferir(perfil)
    eng = _perfis.engine(p)

    def v(chave, padrao=None):
        return _perfis.var(p, chave, padrao)

    cfg = {
        "perfil": p,
        "engine": eng,
        "username": v("username"),
        "password": v("password"),
        "host": v("host"),
        "port": v("port", _PORTAS.get(eng)),
        "database": v("database"),
        "schema": v("schema"),
    }
    if eng == "oracle":
        cfg["service_name"] = v("service_name")
    elif eng == "firebird":
        cfg["charset"] = v("charset", "UTF8")
    elif eng == "mysql":
        cfg["charset"] = v("charset", "utf8mb4")
        # TLS desligado por padrão porque, ligado, ele é EXIGIDO: o PyMySQL recusa
        # servidor sem TLS ("SSL is required but the server doesn't support it"),
        # e cair sozinho para texto puro anularia a proteção. Banco fora da rede
        # interna: db_<perfil>_ssl=1 (e ssl_ca para validar o certificado).
        cfg["ssl"] = (v("ssl", "0") or "").lower() in _VERDADE
        cfg["ssl_ca"] = v("ssl_ca")
    elif eng == "postgres":
        cfg["schema"] = v("schema", "public")
        cfg["sslmode"] = v("sslmode")
    elif eng == "sqlite":
        cfg["database"] = _caminho_sqlite(v("database"))
    return cfg


def engine_do_perfil(perfil=None):
    """Engine: ``oracle``, ``sqlserver``, ``firebird``, ``mysql``, ``postgres`` ou ``sqlite``."""
    return _perfis.engine(perfil)


def teto_de_tempo_s(perfil=None):
    """Segundos que uma consulta pode durar neste perfil (0 = sem teto).

    Configurável por perfil (``db_<perfil>_query_timeout_s``) com um padrão global
    (``db_query_timeout_s``, 180). É o orçamento que o MySQL e o PostgreSQL também
    recebem no próprio servidor; aqui ele se estende aos engines que não têm teto
    de sessão (Oracle, Firebird, SQL Server, SQLite).
    """
    p = _perfis.conferir(perfil)
    bruto = os.getenv(f"db_{p}_query_timeout_s", os.getenv("db_query_timeout_s", "180"))
    try:
        return max(0, int(bruto))
    except (TypeError, ValueError):
        return 180


def _campos_obrigatorios(cfg):
    """Lista de (rótulo, valor) que precisam estar preenchidos, por engine."""
    p = cfg["perfil"]

    def c(chave, rotulo):
        return (f"{rotulo} (db_{p}_{chave})", cfg.get(chave))

    eng = cfg["engine"]
    if eng == "sqlite":
        return [c("database", "arquivo")]
    base = [c("username", "usuário"), c("password", "senha"),
            c("host", "host"), c("port", "porta")]
    if eng == "oracle":
        return base + [c("service_name", "service_name")]
    if eng in ("mysql", "postgres"):
        # Banco não é opcional aqui: no MySQL, sem ele o DATABASE() volta NULL e
        # todo filtro de catálogo por TABLE_SCHEMA para de filtrar; no PostgreSQL,
        # a libpq tentaria um banco com o nome do usuário.
        return base + [c("database", "banco")]
    if eng == "firebird":
        return base + [c("database", "banco/alias")]
    return base  # sqlserver: banco opcional (vazio = o padrão do login)


def credenciais_ok(perfil=None):
    """True se há configuração suficiente para abrir uma conexão no perfil."""
    cfg = _resolver_perfil(perfil)
    return all(valor for _, valor in _campos_obrigatorios(cfg))


def usuario_conexao(perfil=None):
    """Quem conecta (para log). No SQLite não há usuário: mostra o arquivo."""
    cfg = _resolver_perfil(perfil)
    if cfg["engine"] == "sqlite":
        return os.path.basename(cfg.get("database") or "?")
    return cfg["username"]


def _conectar_oracle(cfg):
    """Abre a conexão Oracle e aplica o CURRENT_SCHEMA da sessão, se configurado."""
    import oracledb

    # Thick só quando há Instant Client de verdade: um diretório que não existe
    # faria o init falhar, e o thin atende a maioria dos servidores.
    lib_dir = os.getenv("ORACLE_CLIENT_LIB_DIR")
    if lib_dir and os.path.isdir(lib_dir):
        try:
            oracledb.init_oracle_client(lib_dir=lib_dir)
        except Exception:
            pass  # já inicializado neste processo

    dsn = oracledb.makedsn(cfg["host"], cfg["port"], service_name=cfg["service_name"])
    conn = oracledb.connect(user=cfg["username"], password=cfg["password"], dsn=dsn)

    # Resolve nomes não qualificados no schema dono das tabelas (só afeta esta
    # sessão). `schema` vem do .env, não de input de consulta — mas entra em DDL
    # de sessão, então exigimos identificador simples em vez de confiar.
    schema = cfg.get("schema")
    if schema:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_$#]*$", schema):
            raise RuntimeError(f"db_{cfg['perfil']}_schema inválido: {schema!r}")
        with conn.cursor() as cursor:
            cursor.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {schema}")
    return conn


def _conectar_sqlserver(cfg):
    """Abre a conexão SQL Server via pymssql."""
    import pymssql

    return pymssql.connect(
        server=cfg["host"],
        port=str(cfg["port"]),
        user=cfg["username"],
        password=cfg["password"],
        database=cfg.get("database") or "",
        login_timeout=30,
        # Teto de tempo da QUERY (o login_timeout acima só cobre o handshake). O
        # pymssql não tem cancelamento utilizável de outra thread, então o corte
        # tem de vir do próprio driver.
        timeout=teto_de_tempo_s(cfg["perfil"]),
        # Sem autocommit: só fazemos SELECT, e fechar a conexão descarta a transação.
    )


def _conectar_firebird(cfg):
    """Abre a conexão Firebird via fdb.

    Requer a ``libfbclient`` no host (a imagem Docker instala). Atenção: o ``fdb``
    usa parâmetros posicionais ``?`` (qmark), diferente do Oracle (``:nome``) e do
    pymssql (``%(nome)s``).
    """
    import fdb

    return fdb.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        database=cfg["database"],
        user=cfg["username"],
        password=cfg["password"],
        charset=cfg.get("charset") or "UTF8",
    )


def _sessao_mysql(conn, p):
    """Reforços de sessão, um por um: transação read-only, teto de tempo NO
    SERVIDOR e teto de linhas.

    Um por um porque MySQL e MariaDB divergem no teto de tempo
    (``MAX_EXECUTION_TIME`` em ms × ``max_statement_time`` em segundos): num bloco
    só, a primeira falha levaria os outros reforços junto. O ``read_timeout`` do
    cliente só derruba o socket — a query continuaria queimando CPU do servidor.
    """
    teto_ms = int(_perfis.var(p, "max_execution_ms", "180000"))
    limite = int(_perfis.var(p, "select_limit", "100000"))
    reforcos = [
        ("transação read-only", ["SET SESSION TRANSACTION READ ONLY"]),
        ("teto de tempo", [("SET SESSION MAX_EXECUTION_TIME = %s", (teto_ms,)),
                           ("SET SESSION max_statement_time = %s", (teto_ms / 1000.0,))]),
        ("teto de linhas", [("SET SESSION sql_select_limit = %s", (limite,))]),
    ]
    with conn.cursor() as cur:
        for nome, tentativas in reforcos:
            erro = None
            for t in tentativas:
                sql, args = (t, None) if isinstance(t, str) else t
                try:
                    cur.execute(sql, args)
                    erro = None
                    break
                except Exception as e:  # variante do outro servidor: tenta a próxima
                    erro = e
            if erro is not None:
                print(f"[{p}] reforço de sessão não aplicado ({nome}): {erro}",
                      file=sys.stderr)


def _conectar_mysql(cfg):
    """Abre a conexão MySQL/MariaDB via PyMySQL.

    PyMySQL é Python puro; o ``cryptography`` é que sustenta o TLS e o handshake
    do ``caching_sha2_password``, padrão do MySQL 8.
    """
    import pymysql

    p = cfg["perfil"]
    conn = pymysql.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        user=cfg["username"],
        password=cfg["password"],
        database=cfg["database"],
        charset=cfg.get("charset") or "utf8mb4",
        connect_timeout=30,
        read_timeout=int(_perfis.var(p, "read_timeout", "180")),
        # {"ca": None} liga o TLS sem validar o certificado. Um {} vazio seria
        # falsy e DESLIGARIA o TLS em silêncio.
        ssl={"ca": cfg.get("ssl_ca")} if cfg.get("ssl") else None,
        # Impede LOAD DATA LOCAL INFILE de ler arquivo DESTA máquina.
        local_infile=False,
        autocommit=False,
    )
    _sessao_mysql(conn, p)
    return conn


def _conectar_postgres(cfg):
    """Abre a conexão PostgreSQL via psycopg2.

    Os reforços de SESSÃO vão nas ``options`` (chegam no handshake, antes de
    qualquer query nossa): ``search_path`` no schema do perfil — é o que faz o
    ``current_schema()`` do catálogo apontar para o lugar certo —,
    ``default_transaction_read_only`` e ``statement_timeout``. O
    ``set_session(readonly=True)`` fecha por cima.

    Read-only de sessão é rede de proteção, não permissão de servidor: um usuário
    com DDL poderia desfazê-lo com ``SET``. Quem impede isso é o ``consultar.py``
    (só SELECT/WITH) — e, de preferência, um usuário só-leitura no banco.
    """
    import psycopg2

    p = cfg["perfil"]
    schema = cfg.get("schema") or "public"
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", schema):
        raise RuntimeError(f"db_{p}_schema inválido: {schema!r} "
                           "(use um identificador simples, ex.: public)")
    timeout_ms = int(_perfis.var(p, "statement_timeout_ms", "180000"))
    extras = {"sslmode": cfg["sslmode"]} if cfg.get("sslmode") else {}
    conn = psycopg2.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        user=cfg["username"],
        password=cfg["password"],
        dbname=cfg["database"],
        connect_timeout=30,
        options=f"-c search_path={schema} "
                f"-c default_transaction_read_only=on "
                f"-c statement_timeout={timeout_ms}",
        **extras,
    )
    try:
        conn.set_session(readonly=True)
    except Exception as e:  # as options já cobrem o essencial
        print(f"[{p}] set_session(readonly) não aplicado: {e}", file=sys.stderr)
    return conn


def _conectar_sqlite(cfg):
    """Abre o arquivo SQLite em modo somente leitura.

    Duas travas do próprio SQLite: ``mode=ro`` na URI (o arquivo nem é aberto
    para escrita, e não é criado se não existir) e ``PRAGMA query_only``.
    """
    import pathlib
    import sqlite3

    caminho = cfg["database"]
    if not os.path.isfile(caminho):
        raise RuntimeError(
            f"arquivo SQLite não encontrado: {caminho} "
            "(no banco de exemplo, rode antes: python3 exemplo/criar_banco.py)")
    uri = pathlib.Path(caminho).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.execute("PRAGMA query_only = ON")
    return conn


_CONECTORES = {
    "oracle": _conectar_oracle,
    "sqlserver": _conectar_sqlserver,
    "firebird": _conectar_firebird,
    "mysql": _conectar_mysql,
    "postgres": _conectar_postgres,
    "sqlite": _conectar_sqlite,
}


def create_readonly_connection(perfil=None):
    """Abre uma conexão com o banco do perfil.

    Levanta ``RuntimeError`` com mensagem clara se faltar alguma configuração —
    dizendo QUAL variável preencher.
    """
    cfg = _resolver_perfil(perfil)
    faltando = [rotulo for rotulo, valor in _campos_obrigatorios(cfg) if not valor]
    if faltando:
        raise RuntimeError("Configuração de conexão ausente no .env: " + ", ".join(faltando))
    return _CONECTORES[cfg["engine"]](cfg)
