"""Registro dos perfis — QUAIS bancos esta instalação conhece.

Um perfil é um banco que a ferramenta consulta. Ele existe se estiver listado em
``PERFIS`` no ``.env``, e cada um se configura com variáveis ``db_<perfil>_*``:

    PERFIS=vendas,rh
    PERFIL_PADRAO=vendas
    PERFIS_SENSIVEIS=rh

    db_vendas_engine=postgres
    db_vendas_host=10.0.0.5
    db_vendas_database=erp
    db_vendas_username=leitura
    db_vendas_password=...
    db_vendas_descricao=ERP — pedidos, notas e estoque

Este módulo só sabe o que NÃO é segredo: nome, engine, descrição, banco de
conhecimento e sensibilidade. Usuário e senha são lidos por ``conexao.py`` na hora
de conectar. A separação é o que deixa o painel de acessos e as ferramentas da base
de conhecimento (vetorial, grafo, recuperar, check) rodarem sem driver de banco e
sem credencial nenhuma.

Variáveis globais:

- ``PERFIS`` — lista dos perfis (vírgula ou espaço). Nome em minúsculas,
  ``[a-z][a-z0-9_]*``.
- ``PERFIL_PADRAO`` — o perfil usado quando nenhum é informado (padrão: o 1º).
- ``PERFIS_SENSIVEIS`` — perfis com dado de pessoa (salário, jornada, saúde...).
  O servidor MCP avisa o modelo e o painel não os libera por padrão.
- ``PERFIS_INICIAIS`` — o que um acesso novo recebe no painel (padrão: todos os
  não sensíveis).

Variáveis por perfil (além das de conexão, ver ``conexao.py``):

- ``db_<p>_engine`` — oracle | sqlserver | firebird | mysql | postgres | sqlite.
- ``db_<p>_descricao`` — uma linha sobre o conteúdo; vai para o modelo.
- ``db_<p>_banco`` — chave da base de conhecimento (pasta ``NOTES/<banco>/`` e
  ``contexto/dicionario_<banco>.md``). Padrão: o próprio perfil. Dois perfis que
  enxergam o MESMO schema (ex.: um usuário de leitura e um de BI no mesmo
  servidor) compartilham o banco e, com ele, um dicionário só.
"""

import os
import re
import sys

_AQUI = os.path.dirname(os.path.abspath(__file__))

ENGINES = ("oracle", "sqlserver", "firebird", "mysql", "postgres", "sqlite")
_APELIDOS = {
    "postgresql": "postgres", "pg": "postgres",
    "mssql": "sqlserver", "sql_server": "sqlserver",
    "mariadb": "mysql",
    "sqlite3": "sqlite",
}
_RE_NOME = re.compile(r"^[a-z][a-z0-9_]*$")

# Uma linha de dialeto por engine. Vai para o cabeçalho do dicionário e para o
# roteiro do servidor MCP: é o lembrete que impede o modelo de mandar `LIMIT` para
# o SQL Server ou `TOP` para o PostgreSQL.
DIALETOS = {
    "oracle": "Oracle (ROWNUM ou FETCH FIRST n ROWS ONLY, DUAL, NVL, TO_DATE)",
    "sqlserver": "SQL Server (TOP n, GETDATE, ISNULL; sem LIMIT)",
    "firebird": "Firebird (FIRST n/SKIP, rdb$relations; sem LIMIT)",
    "mysql": "MySQL/MariaDB (LIMIT, IFNULL, DATE_FORMAT; sem DUAL)",
    "postgres": "PostgreSQL (LIMIT, COALESCE, date_trunc; identificador sem "
                "aspas cai para minúsculas)",
    "sqlite": "SQLite (LIMIT, IFNULL, strftime; tipos dinâmicos)",
}


# --------------------------------------------------------------------- .env
def _ler_env_simples(caminho):
    """Leitor mínimo de .env, para quando o python-dotenv não está instalado.

    Existe para as ferramentas de Python puro (recuperar, vetorial, check)
    continuarem rodando com ``python3`` local, sem pip nenhum. Entende
    ``CHAVE=valor``, aspas simples/duplas em volta e comentário de linha inteira.
    Nunca sobrescreve o que já está no ambiente do SO — mesma regra do dotenv.
    """
    try:
        with open(caminho, encoding="utf-8") as fh:
            linhas = fh.readlines()
    except OSError:
        return
    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        if linha.startswith("export "):
            linha = linha[len("export "):]
        chave, _, valor = linha.partition("=")
        chave, valor = chave.strip(), valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "'\"":
            valor = valor[1:-1]
        if chave and chave not in os.environ:
            os.environ[chave] = valor


def _carregar_env():
    """Carrega o primeiro .env encontrado: ao lado do pacote -> pasta acima -> cwd.

    Variáveis já presentes no ambiente do SO sempre têm prioridade (é o que o
    Docker usa: o ``env_file`` do compose chega como ambiente, não como arquivo).

    ``MOTOR_ENV`` troca a busca: um caminho carrega só aquele arquivo (útil com
    mais de uma configuração); ``nenhum`` não carrega arquivo algum (os testes
    usam, para o .env de quem roda não vazar para dentro deles).
    """
    escolhido = (os.getenv("MOTOR_ENV") or "").strip()
    if escolhido.lower() == "nenhum":
        return None
    if escolhido:
        candidatos = [os.path.expanduser(escolhido)]
    else:
        candidatos = [os.path.join(_AQUI, ".env"),
                      os.path.join(os.path.dirname(_AQUI), ".env"),
                      os.path.join(os.getcwd(), ".env")]
    for caminho in dict.fromkeys(candidatos):
        if not os.path.isfile(caminho):
            continue
        try:
            from dotenv import load_dotenv
            load_dotenv(caminho)
        except ImportError:
            _ler_env_simples(caminho)
        return caminho
    return None


ARQUIVO_ENV = _carregar_env()


# ------------------------------------------------------------------ registro
def _lista(bruto):
    """'a, b  c' -> ('a', 'b', 'c'), minúsculo e sem repetição, na ordem dada."""
    itens = [x.strip().lower() for x in re.split(r"[,\s]+", bruto or "") if x.strip()]
    return tuple(dict.fromkeys(itens))


def _perfis():
    validos = []
    for nome in _lista(os.getenv("PERFIS")):
        if _RE_NOME.match(nome):
            validos.append(nome)
        else:
            print(f"[perfis] nome de perfil inválido ignorado: {nome!r} "
                  "(use minúsculas, dígitos e _, começando por letra)", file=sys.stderr)
    return tuple(validos)


PERFIS = _perfis()


def _padrao():
    pedido = (os.getenv("PERFIL_PADRAO") or "").strip().lower()
    if pedido and pedido in PERFIS:
        return pedido
    if pedido:
        print(f"[perfis] PERFIL_PADRAO={pedido!r} não está em PERFIS; "
              "usando o primeiro da lista", file=sys.stderr)
    return PERFIS[0] if PERFIS else ""


PERFIL_PADRAO = _padrao()
SENSIVEIS = tuple(p for p in _lista(os.getenv("PERFIS_SENSIVEIS")) if p in PERFIS)
# Vazio conta como "não definido": o compose repassa a variável ao painel mesmo
# quando ela não está no .env, e aí chega como string vazia. Para um acesso novo
# nascer sem banco nenhum, use PERFIS_INICIAIS=nenhum.
_iniciais = (os.getenv("PERFIS_INICIAIS") or "").strip()
if _iniciais.lower() == "nenhum":
    INICIAIS = ()
elif _iniciais:
    INICIAIS = tuple(p for p in _lista(_iniciais) if p in PERFIS)
else:
    # Menor privilégio por padrão: acesso novo nasce sem os bancos sensíveis, e
    # liberar um deles é decisão explícita de quem administra.
    INICIAIS = tuple(p for p in PERFIS if p not in SENSIVEIS)


def var(perfil, chave, padrao=None):
    """Valor de ``db_<perfil>_<chave>``, sem espaços nas pontas; vazio vira ``padrao``."""
    valor = os.getenv(f"db_{perfil}_{chave}")
    if valor is None:
        return padrao
    valor = valor.strip()
    return valor if valor else padrao


def conferir(perfil=None):
    """Normaliza o nome e garante que o perfil existe. Devolve o nome.

    Levanta ``ValueError`` com a lista do que existe — é a mensagem que o modelo
    lê quando chuta um perfil, então ela precisa dizer qual é o certo.
    """
    nome = (perfil or PERFIL_PADRAO or "").strip().lower()
    if not PERFIS:
        raise ValueError(
            "nenhum perfil configurado: defina PERFIS no .env (veja .env.example)")
    if nome not in PERFIS:
        raise ValueError(
            f"perfil desconhecido: {nome!r} (configurados: {', '.join(PERFIS)})")
    return nome


def engine(perfil=None):
    """Engine do perfil, já normalizado. ``RuntimeError`` se faltar ou for inválido."""
    nome = conferir(perfil)
    bruto = (var(nome, "engine") or "").lower()
    eng = _APELIDOS.get(bruto, bruto)
    if eng not in ENGINES:
        raise RuntimeError(
            f"db_{nome}_engine {'ausente' if not bruto else f'inválido ({bruto!r})'} "
            f"— use um de: {', '.join(ENGINES)}")
    return eng


def banco(perfil):
    """Chave da base de conhecimento do perfil (pasta de NOTES e dicionário).

    Aceita nome que não é perfil (devolve ele mesmo): o ``check.py`` roda no CI,
    sem .env, e precisa tratar a pasta ``NOTES/<banco>/`` sem conhecer perfil.
    """
    nome = (perfil or "").strip().lower()
    if nome not in PERFIS:
        return nome
    chave = (var(nome, "banco") or nome).lower()
    if not _RE_NOME.match(chave):
        raise RuntimeError(f"db_{nome}_banco inválido: {chave!r}")
    return chave


def perfis_do_banco(chave):
    """Perfis que leem o mesmo banco de conhecimento (na ordem de PERFIS)."""
    return tuple(p for p in PERFIS if banco(p) == chave)


def descricao(perfil):
    return var(perfil, "descricao", "") or ""


def rotulo(perfil):
    """Nome curto para tela; padrão é o próprio perfil."""
    return var(perfil, "rotulo", perfil) or perfil


def sensivel(perfil):
    return perfil in SENSIVEIS


def dialeto(eng):
    return DIALETOS.get(eng, eng)


def resumo():
    """[{perfil, engine, descricao, sensivel, banco}] — para montar texto de ajuda.

    Engine ausente/inválido não derruba o resumo (vira '?'): quem chama isto é
    texto de ajuda e instrução do servidor, que não podem morrer por config
    incompleta de UM perfil.
    """
    saida = []
    for p in PERFIS:
        try:
            eng = engine(p)
        except RuntimeError:
            eng = "?"
        saida.append({"perfil": p, "engine": eng, "descricao": descricao(p),
                      "sensivel": sensivel(p), "banco": banco(p)})
    return saida
