#!/usr/bin/env python3
"""Servidor MCP — expõe as ferramentas read-only como tools MCP.

Reaproveita a MESMA camada da CLI (consultar.py), a recuperação (recuperar.py) e
o guardrail (validar_sql.py). É consumido por qualquer cliente MCP (Claude Code,
Claude Desktop): o modelo do cliente chama estas tools; ESTE servidor executa as
consultas read-only no banco. As credenciais (.env) ficam só aqui — quem usa
recebe acesso às ferramentas, nunca ao banco cru.

Em HTTP, só atende requisição que venha do proxy autenticado: o Caddyfile injeta
X-Proxy-Segredo e o GateDoProxy (middleware ASGI) responde 403 sem ele. É isso
que torna confiáveis os headers do parágrafo seguinte.

Cada pessoa só alcança os bancos liberados para ela: o proxy injeta os perfis no
header X-Db-Perfis (administrado no painel de acessos) e as tools de consulta
recusam o resto. Ver _perfis_liberados() abaixo.

Duas gravações saem daqui, com propósitos que não se misturam:
  - auditoria.py  -> quem consultou o quê: todo SQL, inclusive recusa e erro
  - perguntas.py  -> o que as pessoas querem saber e o que a base entregou, para
                     virar nota nova: só a pergunta e a query que funcionou

Tools expostas (todas anotadas como read-only):
  - listar_indice()                        -> índice global (tabelas + NOTES com caminho)
  - recuperar_contexto(pergunta)           -> subgrafo relevante (schema + NOTES)
  - validar_sql(sql, perfil)               -> problemas de schema (guardrail)
  - consultar_sql(sql, perfil, max_linhas) -> executa SELECT/WITH read-only

Prompt publicado:
  - pergunta(pergunta)                     -> o roteiro recuperar -> validar -> consultar

Resources publicadas (tool = ação, resource = dado):
  - nota://<banco>/<assunto>/<slug>        -> uma nota da base, uma resource cada
  - dicionario://<perfil>/<tabela>         -> schema de uma tabela (template)

Falha de chamada vira ERRO de protocolo (``isError``), via ToolError — e não texto
de erro dentro de uma resposta de sucesso, que o modelo às vezes lê como dado.
Exceção proposital: ``validar_sql`` devolvendo "PROBLEMAS: ..." é SUCESSO — achar
problema é o trabalho dela.

Rodar em STDIO (uso local, uma pessoa):
    python3 mcp_server.py
    # no cliente:  claude mcp add motor-consulta -- python3 /CAMINHO/mcp_server.py

Rodar em HTTP (máquina compartilhada, SEMPRE atrás do proxy):
    MCP_TRANSPORT=http MCP_HOST=0.0.0.0 MCP_PORT=8000 python3 mcp_server.py
"""

import csv
import datetime
import functools
import hmac
import io
import os
import sys
import time

import anyio.to_thread

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations

try:  # como pacote
    from .consultar import _validar_select, _executar
    from . import perfis as _perfis
    from . import recuperar
    from . import validar_sql as _guard
    from . import auditoria
    from . import perguntas
except ImportError:  # como script direto
    from consultar import _validar_select, _executar
    import perfis as _perfis
    import recuperar
    import validar_sql as _guard
    import auditoria
    import perguntas

PERFIS = _perfis.PERFIS
PERFIL_PADRAO = _perfis.PERFIL_PADRAO
NOME_SERVIDOR = os.environ.get("MCP_NOME", "motor-consulta")
_AQUI = os.path.dirname(os.path.abspath(__file__))

# `stateless_http`: um transporte novo por requisição, sem sessão.
#
# O modo com sessão (padrão do SDK) mantém um `GET /mcp` aberto por horas — o
# stream SSE por onde o servidor empurraria mensagens ao cliente. Quando o caminho
# de rede corta esse TCP sem encerramento limpo (timeout de ocioso em NAT/firewall:
# o servidor não recebe FIN nem RST), o servidor continua com o stream
# registrado, e a reconexão do cliente com o mesmo Mcp-Session-Id leva
# 409 "Conflict: Only one SSE stream is allowed per session". O mcp-remote tenta
# algumas vezes, desiste, e a ferramenta some até o app reiniciar.
#
# Dá para abrir mão da sessão: as tools são read-only e síncronas, e o servidor
# nunca empurra nada para o cliente. O que se perde é o Mcp-Session-Id, que o
# _ULTIMA_PERGUNTA usa para amarrar a pergunta à query — cai para agrupar por
# pessoa ("agrupa pior, nunca mistura gente diferente").
#
# MCP_STATELESS=0 volta ao modo com sessão.
MCP_STATELESS = os.environ.get("MCP_STATELESS", "1").strip().lower() not in (
    "0", "false", "nao", "não", "",
)


def _lista_de_perfis(com_dialeto=False):
    """Uma linha por perfil configurado, para o modelo saber o que existe."""
    linhas = []
    for r in _perfis.resumo():
        partes = [f"- `{r['perfil']}`"]
        if com_dialeto:
            partes.append(f"— {_perfis.dialeto(r['engine'])}")
        else:
            partes.append(f"({r['engine']})")
        if r["descricao"]:
            partes.append(f"— {r['descricao']}")
        if r["sensivel"]:
            partes.append("— **dado de pessoa: traga agregado**")
        linhas.append(" ".join(partes))
    return "\n".join(linhas) or "- (nenhum perfil configurado no servidor)"


def _instrucoes_extras():
    """Regras do domínio, escritas à mão em contexto/instrucoes.md (opcional).

    É onde entra o que só quem conhece o negócio sabe: quais recortes são
    ambíguos (empresa, filial, centro de custo), o que nunca trazer, qual banco
    responde o quê. Vai nas `instructions`, então custa token em TODA sessão:
    mantenha curto.
    """
    caminho = os.path.join(_AQUI, "contexto", "instrucoes.md")
    try:
        with open(caminho, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


# `instructions` — o único canal que NÃO depende da pessoa lembrar.
#
# Prompt e resource são opt-in: quem escolhe é a pessoa. `instructions` vai na
# resposta do `initialize` e o cliente injeta no contexto da sessão. É curto de
# propósito (carrega em toda sessão): diz o que o servidor é, que bancos existem
# e as regras de conduta.
INSTRUCOES = f"""Investigação SOMENTE LEITURA em bancos de dados. Perfis disponíveis:

{_lista_de_perfis()}

Antes de escrever SQL, chame `recuperar_contexto` — é o que evita inventar nome de
tabela ou de coluna. Depois `consultar_sql`. Cite sempre a query e o `perfil`:
número sem query não se confere. Em pergunta ambígua de período ou de recorte,
pergunte antes de escolher.

Traga agregado quando a pergunta permitir e nunca despeje dado pessoal (documento,
salário, contato) sem necessidade — resposta circula mais fácil que consulta."""

_EXTRAS = _instrucoes_extras()
if _EXTRAS:
    INSTRUCOES += "\n\n" + _EXTRAS

mcp = FastMCP(
    NOME_SERVIDOR,
    instructions=INSTRUCOES,
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8000")),
    stateless_http=MCP_STATELESS,
)

# --------------------------------------------------------------------------
# Provar que a requisição PASSOU pelo proxy
#
# O gate de bancos (abaixo) confia em `X-Db-Perfis`, com o argumento de que o
# proxy usa `header_up`, que é SET — então o cliente não forja. O argumento é
# certo e INCOMPLETO: só vale para quem passa pelo proxy. Quem alcança a porta do
# mcp DIRETO (outro container da mesma rede, qualquer processo do host) mandaria
# `X-Db-Perfis: *` por conta própria e consultaria todos os bancos sem token — e
# ainda escolheria o nome que vai para a trilha de auditoria.
#
# A correção é um segredo compartilhado que SÓ o proxy tem: o Caddyfile injeta
# `request_header X-Proxy-Segredo "{env.MCP_PROXY_SEGREDO}"` em toda requisição,
# e o middleware abaixo recusa qualquer HTTP que não o traga. `request_header` é
# SET (delete + add): o valor que o cliente mandar é descartado antes de chegar
# aqui. Fica no Caddyfile, e não no bloco de cada pessoa, para que nenhum bloco
# novo — ou escrito à mão — possa esquecer a linha.
#
# É no TRANSPORTE, e não em cada tool, de propósito: cobre tools, resources,
# prompts e o próprio `initialize`, e nenhuma tool futura pode esquecer de
# checar. O stdio (CLI local) não passa por aqui.
#
# Segredo vazio + transporte HTTP = NEGA TUDO, e não "libera tudo": config que
# não chegou nunca vira permissão.
PROXY_SEGREDO = os.environ.get("MCP_PROXY_SEGREDO", "").strip()
HEADER_SEGREDO = os.environ.get("MCP_HEADER_SEGREDO", "X-Proxy-Segredo")


class GateDoProxy:
    """Middleware ASGI: HTTP só entra com o segredo que o proxy injeta.

    ASGI puro, e não `BaseHTTPMiddleware` do Starlette: o transporte
    streamable-http responde em SSE, e o BaseHTTPMiddleware sabidamente
    atropela resposta em streaming.
    """

    def __init__(self, app, segredo: str, cabecalho: str):
        self._app = app
        self._esperado = segredo.encode()
        self._cabecalho = cabecalho.lower().encode()

    def _autorizado(self, scope) -> bool:
        if not self._esperado:
            return False
        for nome, valor in scope.get("headers") or ():
            if nome.lower() == self._cabecalho:
                return hmac.compare_digest(valor, self._esperado)
        return False

    async def __call__(self, scope, receive, send):
        # `lifespan` passa direto: é ele que sobe o session manager do
        # transporte. Autenticação é assunto de requisição HTTP.
        if scope["type"] != "http" or self._autorizado(scope):
            await self._app(scope, receive, send)
            return
        corpo = (b"acesso somente pelo proxy autenticado\n" if self._esperado else
                 b"MCP_PROXY_SEGREDO ausente: este servidor nao atende HTTP\n")
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(corpo)).encode())]})
        await send({"type": "http.response.body", "body": corpo})


# --------------------------------------------------------------------------
# Quais bancos esta requisição pode consultar
#
# O servidor MCP não autentica PESSOA: quem autentica é o proxy (Caddy), com um
# Bearer por pessoa. Para que ele também decida QUAIS bancos cada pessoa alcança,
# o bloco da pessoa no `caddy/tokens.caddy` injeta um header com os perfis
# liberados (`header_up X-Db-Perfis "vendas,estoque"`), administrado pelo painel.
# Como o catch-all do Caddyfile devolve 401, nenhuma requisição chega aqui sem
# passar pelo bloco de alguém.
#
# Sem header (uso local em stdio, ou bloco escrito à mão sem a linha) vale
# MCP_PERFIS_PADRAO. No compose, o serviço `mcp` fixa isso em VAZIO — nega tudo,
# em vez de liberar tudo, quando a atribuição não chega. A CLI não passa por aqui.
HEADER_PERFIS = os.environ.get("MCP_HEADER_PERFIS", "X-Db-Perfis")
# Rótulo da pessoa, injetado pelo bloco dela no proxy (mesmo mecanismo).
HEADER_PESSOA = os.environ.get("MCP_HEADER_PESSOA", "X-Db-Pessoa")
PERFIS_PADRAO = os.environ.get("MCP_PERFIS_PADRAO", ",".join(PERFIS))
# Vencimento do acesso, injetado pelo bloco da pessoa. Sem a linha o acesso não
# vence — o painel mostra isso como pendência.
HEADER_EXPIRA = os.environ.get("MCP_HEADER_EXPIRA", "X-Db-Expira")


def _header(nome):
    """Valor de um header da requisição HTTP ATUAL (None em stdio)."""
    try:
        pedido = mcp.get_context().request_context.request
        return pedido.headers.get(nome) if pedido is not None else None
    except Exception:
        return None  # fora de uma requisição (import, teste)


def _pessoa() -> str:
    """Rótulo de quem chamou, para a trilha.

    Distingue os dois "sem header": `local` é uso em stdio/CLI (não há requisição
    HTTP); `desconhecido` é requisição que passou pelo proxy mas cujo bloco não
    injeta X-Db-Pessoa — bug de configuração, que tem de aparecer na trilha em vez
    de se disfarçar de uso local.
    """
    v = (_header(HEADER_PESSOA) or "").strip()
    if v:
        return v
    try:
        tem_requisicao = mcp.get_context().request_context.request is not None
    except Exception:
        tem_requisicao = False
    return "desconhecido" if tem_requisicao else "local"


# --------------------------------------------------------------------------
# Amarrar a pergunta à query que a respondeu
#
# O servidor não vê o chat: a pergunta em linguagem natural só cruza a fronteira
# nos argumentos de `recuperar_contexto` e do prompt `pergunta`. O
# `consultar_sql` recebe SQL e mais nada. Para o catálogo poder guardar
# "pergunta -> query que funcionou", guardamos aqui a última pergunta de cada
# SESSÃO MCP (header `Mcp-Session-Id`); sem sessão, a chave cai para a pessoa.
#
# É memória de processo, de propósito: um restart esquece o que estava em voo, e
# a pior consequência é uma query entrar no catálogo sem pergunta amarrada — que
# é o mesmo que não entrar.
HEADER_SESSAO = os.environ.get("MCP_HEADER_SESSAO", "Mcp-Session-Id")
_ULTIMA_PERGUNTA = {}
_ULTIMA_MAX = 500   # teto: sessão nova não pode fazer o processo crescer sem fim


def _sessao():
    """Identificador da sessão MCP atual (None em stdio/CLI)."""
    v = (_header(HEADER_SESSAO) or "").strip()
    return v[:16] or None


def _lembrar(pergunta) -> None:
    chave = _sessao() or _pessoa()
    if chave in _ULTIMA_PERGUNTA:
        del _ULTIMA_PERGUNTA[chave]          # reinsere no fim (dict é ordenado)
    elif len(_ULTIMA_PERGUNTA) >= _ULTIMA_MAX:
        del _ULTIMA_PERGUNTA[next(iter(_ULTIMA_PERGUNTA))]   # descarta a mais velha
    _ULTIMA_PERGUNTA[chave] = pergunta


def _pergunta_da_sessao():
    return _ULTIMA_PERGUNTA.get(_sessao() or _pessoa())


def _venceu() -> str:
    """Data de vencimento se o acesso JÁ venceu; string vazia se está em dia.

    Comparação de texto entre datas ISO, que ordena igual à data — o container
    roda com o TZ do compose, então "hoje" é o hoje de quem consulta. O dia do
    vencimento vale inteiro: só vence no dia seguinte.
    """
    ate = (_header(HEADER_EXPIRA) or "").strip()[:10]
    if len(ate) != 10:
        return ""
    hoje = datetime.datetime.now().strftime("%Y-%m-%d")
    return ate if hoje > ate else ""


def _perfis_liberados() -> set:
    """Perfis que a requisição atual pode consultar."""
    bruto = _header(HEADER_PERFIS)
    if bruto is None:
        bruto = PERFIS_PADRAO
    if bruto.strip() == "*":
        return set(PERFIS)
    return {p.strip().lower() for p in bruto.split(",") if p.strip()} & set(PERFIS)


def _vencido_msg(ate: str) -> str:
    return (
        f"ACESSO VENCIDO: seu acesso expirou em {ate} e não consulta mais nenhum "
        f"banco. Peça a renovação a quem administra o painel de acessos — o token "
        f"continua o mesmo, é só o prazo que venceu. Não tente outro perfil nem "
        f"outro token."
    )


def _negado(perfil: str, liberados: set) -> str:
    """Mensagem de recusa — diz ao modelo o que pedir, em vez de insistir."""
    if not liberados:
        return (
            f"ACESSO NEGADO: seu acesso não tem nenhum banco liberado (perfil pedido: "
            f"'{perfil}'). Peça a liberação a quem administra o painel de acessos."
        )
    return (
        f"ACESSO NEGADO ao banco '{perfil}'. Seu acesso permite: "
        f"{', '.join(sorted(liberados))}. Se precisa deste banco, peça a liberação — "
        "não tente contornar por outro perfil."
    )


def _normalizar_perfil(perfil):
    return ((perfil or PERFIL_PADRAO) or "").strip().lower()


# Catálogo do dicionário por perfil (para o guardrail). Carregado uma vez, no boot
# — atualizar o dicionário exige reiniciar o serviço, como as NOTES. Perfil sem
# dicionário gerado fica com {} e o guardrail libera (fail-open).
_CATALOGOS = {}
for _perfil in PERFIS:
    try:
        _CATALOGOS[_perfil] = _guard.carregar_catalogo(_perfil)
    except Exception:
        _CATALOGOS[_perfil] = {}
print("guardrail: " + (", ".join(f"{p}={len(c)} tabelas" for p, c in _CATALOGOS.items())
                       or "nenhum perfil configurado"), file=sys.stderr)


def _csv(colunas, linhas, truncado, max_linhas):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(colunas)
    for row in linhas:
        w.writerow(["" if v is None else v for v in row])
    out = buf.getvalue()
    if truncado:
        out += f"\n[resultado truncado em {max_linhas} linhas — agregue ou filtre mais]"
    return out


@mcp.tool(
    title="Índice da base de conhecimento",
    annotations=ToolAnnotations(title="Índice da base de conhecimento",
                                readOnlyHint=True, openWorldHint=False),
)
def listar_indice() -> str:
    """Índice global: todas as tabelas conhecidas (nome + comentário) e as NOTES
    (título + caminho em NOTES/<banco>/<assunto>/, agrupadas por banco e
    assunto). Use para descobrir o que existe antes de detalhar."""
    try:
        return recuperar.indice_global()
    except Exception as e:
        raise ToolError(f"não foi possível montar o índice: {e}")


@mcp.tool(
    title="Recuperar contexto para uma pergunta",
    annotations=ToolAnnotations(title="Recuperar contexto para uma pergunta",
                                readOnlyHint=True, openWorldHint=False),
)
def recuperar_contexto(pergunta: str) -> str:
    """Subgrafo relevante à pergunta: schema (colunas) das tabelas + corpo das
    NOTES ligadas (cada bloco marcado com o caminho da nota). Seleção pelo índice
    vetorial. Chame ANTES de escrever SQL, para usar os nomes/regras certos."""
    try:
        r, tabelas, notas, forca = recuperar.recuperar_detalhado(pergunta)
    except Exception as e:
        raise ToolError(f"falha na recuperação: {e}")
    # O catálogo guarda a pergunta COM o que a base entregou: pergunta que volta
    # sem contexto — ou com contexto FRACO — é nota a escrever (ver perguntas.py).
    # Fora do gate de perfil, como o resto da base de conhecimento.
    perguntas.registrar(_pessoa(), pergunta, "recuperar_contexto",
                        tabelas=tabelas, notas=notas, sessao=_sessao(),
                        forca=forca)
    _lembrar(pergunta)
    # Nada relevante não é falha: é resposta (e a dica de como insistir).
    return r or "(nada relevante — tente termos diferentes ou listar_indice)"


@mcp.tool(
    title="Validar SQL contra o schema",
    # openWorldHint: a checagem de existência de tabela vai ao banco ao vivo.
    annotations=ToolAnnotations(title="Validar SQL contra o schema",
                                readOnlyHint=True, openWorldHint=True),
)
def validar_sql(sql: str, perfil: str = PERFIL_PADRAO) -> str:
    """Valida os identificadores do SELECT contra o schema do perfil (colunas
    descritas no dicionário + existência de tabela no catálogo do banco).
    Retorna 'OK' ou a lista de problemas."""
    perfil = _normalizar_perfil(perfil)
    venceu = _venceu()
    if venceu:
        auditoria.registrar(_pessoa(), perfil, "validar_sql", ok=False, sql=sql,
                            motivo="acesso_vencido", erro=f"venceu em {venceu}")
        raise ToolError(_vencido_msg(venceu))
    liberados = _perfis_liberados()
    if perfil not in liberados:
        auditoria.registrar(_pessoa(), perfil, "validar_sql", ok=False, sql=sql,
                            motivo="perfil_negado")
        raise ToolError(_negado(perfil, liberados))
    try:
        problemas = _guard.validar_schema(sql, _CATALOGOS.get(perfil, {}), perfil)
    except Exception as e:
        raise ToolError(f"falha na validação: {e}")
    return "OK — nenhum identificador desconhecido." if not problemas \
        else "PROBLEMAS:\n- " + "\n- ".join(problemas)


_DESCRICAO_CONSULTAR = f"""Executa uma consulta SELECT/WITH READ-ONLY e devolve o resultado em CSV.

Perfis (passe em `perfil`; padrão `{PERFIL_PADRAO or '—'}`):
{_lista_de_perfis(com_dialeto=True)}

Recusa qualquer coisa que não seja SELECT/WITH e passa pelo guardrail de schema.
Cada pessoa só alcança os bancos liberados para ela no painel de acessos."""


@mcp.tool(
    title="Consultar o banco (somente SELECT)",
    description=_DESCRICAO_CONSULTAR,
    annotations=ToolAnnotations(title="Consultar o banco (somente SELECT)",
                                readOnlyHint=True, openWorldHint=True),
)
async def consultar_sql(sql: str, perfil: str = PERFIL_PADRAO, max_linhas: int = 200) -> str:
    pessoa = _pessoa()
    perfil = _normalizar_perfil(perfil)
    # Vencimento antes do perfil: quem venceu não consulta banco nenhum, e a
    # mensagem tem de dizer POR QUE — "peça a liberação" mandaria a pessoa pedir
    # a coisa errada.
    venceu = _venceu()
    if venceu:
        auditoria.registrar(pessoa, perfil, "consultar_sql", ok=False, sql=sql,
                            motivo="acesso_vencido", erro=f"venceu em {venceu}")
        raise ToolError(_vencido_msg(venceu))
    liberados = _perfis_liberados()
    if perfil not in liberados:
        auditoria.registrar(pessoa, perfil, "consultar_sql", ok=False, sql=sql,
                            motivo="perfil_negado")
        raise ToolError(_negado(perfil, liberados))

    try:
        sql_ok = _validar_select(sql)
    except Exception as e:
        auditoria.registrar(pessoa, perfil, "consultar_sql", ok=False, sql=sql,
                            motivo="nao_e_select", erro=e)
        raise ToolError(f"só SELECT/WITH é permitido: {e}")

    try:
        problemas = _guard.validar_schema(sql_ok, _CATALOGOS.get(perfil, {}), perfil)
    except Exception:
        problemas = []  # fail-open: guardrail nunca barra por erro próprio
    if problemas:
        auditoria.registrar(pessoa, perfil, "consultar_sql", ok=False, sql=sql_ok,
                            motivo="guardrail_schema", erro="; ".join(problemas))
        raise ToolError("RECUSADO pelo guardrail de schema (corrija e tente de novo):\n- "
                        + "\n- ".join(problemas))

    inicio = time.monotonic()
    try:
        # FORA DO EVENT LOOP, de propósito: o FastMCP chama tool síncrona INLINE
        # no loop, e uma consulta que pendura no banco pararia o servidor para
        # TODO MUNDO. Com `to_thread`, uma consulta lenta atrasa só quem a fez.
        #
        # O teto de tempo que ENCERRA a consulta está em `consultar._Watchdog`:
        # sem ele a thread e a conexão ficariam penduradas até o banco responder.
        # As duas metades são necessárias — uma não substitui a outra.
        #
        # `_pessoa()`/`_sessao()` são lidos AQUI, no loop: vêm do request_context,
        # que é um contextvar e não atravessa `to_thread`.
        colunas, linhas, truncado = await anyio.to_thread.run_sync(
            functools.partial(_executar, sql_ok, max_linhas=max_linhas, perfil=perfil)
        )
    except TimeoutError as e:
        auditoria.registrar(pessoa, perfil, "consultar_sql", ok=False, sql=sql_ok,
                            motivo="estourou_o_tempo", erro=e,
                            ms=(time.monotonic() - inicio) * 1000)
        raise ToolError(str(e))
    except Exception as e:
        auditoria.registrar(pessoa, perfil, "consultar_sql", ok=False, sql=sql_ok,
                            motivo="erro_no_banco", erro=e,
                            ms=(time.monotonic() - inicio) * 1000)
        raise ToolError(f"erro ao executar no banco: {e}")
    ms = (time.monotonic() - inicio) * 1000
    auditoria.registrar(pessoa, perfil, "consultar_sql", ok=True, sql=sql_ok,
                        linhas=len(linhas), truncado=truncado, ms=ms)
    # Só a query que DEU CERTO, e só se há pergunta conhecida na sessão: no
    # catálogo ela vale como resposta que funcionou (o rascunho do exemplo SQL da
    # nota futura). Recusa e erro de banco ficam na trilha de auditoria, acima.
    perguntas.registrar_sql(pessoa, _pergunta_da_sessao(), perfil, sql_ok,
                            linhas=len(linhas), ms=ms, sessao=_sessao())
    return _csv(colunas, linhas, truncado, max_linhas)


# --------------------------------------------------------------------------
# Prompt — o roteiro de investigação, para qualquer cliente
#
# Terceira primitiva do MCP (tool = ação, resource = dado, prompt = roteiro
# reutilizável). O mesmo fluxo existe em `.claude/commands/pergunta.md` para quem
# abre ESTE repositório no Claude Code; como prompt do servidor, ele chega a quem
# usa o Desktop, que não tem repo nem CLI. O texto é outro de propósito: lá os
# passos são comandos de CLI, aqui são as tools.
# --------------------------------------------------------------------------
@mcp.prompt(
    name="pergunta",
    title="Responder uma pergunta de negócio nos bancos",
    description=("Roteiro read-only: recupera o contexto da base de conhecimento, "
                 "valida o SQL contra o schema e só então consulta o banco."),
)
def pergunta(pergunta: str) -> str:
    # A redação da PESSOA, não a do modelo — é o que o catálogo tem de mais
    # próximo das palavras de quem perguntou (ver perguntas.py).
    perguntas.registrar(_pessoa(), pergunta, "prompt", sessao=_sessao())
    _lembrar(pergunta)
    extras = f"\n## Regras deste domínio\n\n{_EXTRAS}\n" if _EXTRAS else ""
    return f"""Responda à pergunta de negócio abaixo consultando os bancos, direto ao
ponto. É tudo SOMENTE LEITURA.

## Roteiro

1. `recuperar_contexto("{pergunta}")` — pega o schema das tabelas certas e as
   regras/armadilhas já registradas. **Sempre antes de escrever SQL**: é o que
   evita inventar nome de tabela ou de coluna.
   - Veio pouco? Tente outras palavras, ou `listar_indice()` para ver o que existe.
   - Quer ler a nota inteira? Ela é um endereço: `nota://<banco>/<assunto>/<slug>`.
2. Monte o SELECT no dialeto do banco:
{_lista_de_perfis(com_dialeto=True)}
3. Em query grande ou nova, `validar_sql(sql, perfil)` antes de executar — ele
   confere coluna contra o dicionário e tabela contra o catálogo do banco.
4. `consultar_sql(sql, perfil)` executa. Se vier RECUSADO pelo guardrail,
   corrija o identificador apontado em vez de tentar outro caminho.
5. Responda citando a query usada e o `perfil`. Número sem query não se confere.

## Antes de chutar, pergunte

Se a pergunta for ambígua em **período** ou **recorte** (unidade, filial, projeto,
cliente), pergunte antes de escolher por conta própria — o número muda inteiro
conforme o recorte.

## Limites

- Só SELECT/WITH: o servidor recusa o resto, e não existe caminho alternativo.
- Consulta com teto de tempo (é banco de produção): agregue e filtre cedo.
- Se o seu acesso não cobre o banco necessário (ACESSO NEGADO), diga isso a quem
  perguntou e oriente a pedir a liberação — não tente por outro perfil.
- Perfil marcado como dado de pessoa: traga agregado quando a pergunta permitir;
  nunca despeje documento, salário ou contato sem necessidade.
{extras}
## Pergunta

{pergunta}
"""


# --------------------------------------------------------------------------
# Resources — o caminho da informação virou endereço
#
# Uma resource por nota, com o caminho NOTES/<banco>/<assunto>/<slug>.md virando o
# URI nota://<banco>/<assunto>/<slug>. O que isso dá, que a tool não dava:
# `resources/list` JÁ É o mapa da base (o cliente navega sem gastar chamada de
# tool) e a pessoa pode anexar uma nota específica ao contexto por conta própria.
#
# O schema das tabelas vai como TEMPLATE (dicionario://<perfil>/<tabela>): listar
# um por um só inflaria o `resources/list`.
#
# A lista é montada no BOOT, como o dicionário — nota nova exige reiniciar o
# serviço. E resource não passa pelo gate de perfil, igual a `listar_indice` e
# `recuperar_contexto`: a base de conhecimento (schema + regras) é comum a todos;
# o que o gate protege é a CONSULTA aos dados.
# --------------------------------------------------------------------------
def _leitor(nid):
    """Função sem parâmetro que lê a nota `nid`.

    O FastMCP exige que os parâmetros da função casem com os do URI — e o URI de
    uma resource concreta não tem nenhum. Daí a fábrica: `nid` fica no
    fechamento, não na assinatura.
    """
    def _ler():
        corpo = recuperar.corpo_nota(nid)
        if corpo is None:
            raise ResourceError(f"nota não encontrada: {nid}")
        return corpo
    return _ler


def _publicar_notas():
    """Registra uma resource por nota. Devolve quantas foram publicadas."""
    try:
        notas = recuperar.listar_notas()
    except Exception as exc:            # sem índice, o servidor sobe sem resources
        print(f"resources: NOTES não publicadas ({exc})", file=sys.stderr)
        return 0

    publicadas = 0
    for n in notas:
        if not (n["perfil"] and n["assunto"]):
            continue                    # nota fora de <banco>/<assunto> não tem endereço

        mcp.resource(
            f"nota://{n['perfil']}/{n['assunto']}/{n['id']}",
            name=n["id"],
            title=n["titulo"][:100],
            description=f"[{n['perfil']}/{n['assunto']}] {n['titulo'][:400]}",
            mime_type="text/markdown",
        )(_leitor(n["id"]))
        publicadas += 1
    return publicadas


_NOTAS_PUBLICADAS = _publicar_notas()
print(f"resources: {_NOTAS_PUBLICADAS} notas em nota://<banco>/<assunto>/<slug>",
      file=sys.stderr)


@mcp.resource(
    "dicionario://{perfil}/{tabela}",
    name="schema_da_tabela",
    title="Schema de uma tabela",
    description=("Colunas e tipos de uma tabela/view no banco indicado. "
                 f"perfil: {' | '.join(PERFIS) or '—'}."),
    mime_type="text/markdown",
)
def schema_da_tabela(perfil: str, tabela: str) -> str:
    bloco = recuperar.schema_tabela(tabela, perfil)
    if not bloco:
        raise ResourceError(
            f"{tabela} não está no dicionário de {perfil} — use listar_indice "
            "para ver o que existe, ou descreva a tabela pela CLI e regenere."
        )
    return bloco


if __name__ == "__main__":
    transporte = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transporte in ("http", "streamable-http"):
        print(f"MCP {NOME_SERVIDOR} em HTTP :{os.environ.get('MCP_PORT', '8000')}",
              file=sys.stderr)
        if PROXY_SEGREDO:
            print(f"gate do proxy ATIVO: exijo {HEADER_SEGREDO} em toda requisição HTTP",
                  file=sys.stderr)
        else:
            print("[ATENÇÃO] MCP_PROXY_SEGREDO vazio: NEGO toda requisição HTTP "
                  "(fail closed). Preencha no .env e recrie o serviço.", file=sys.stderr)
        # `mcp.run(transport=...)` monta este mesmo app e o serve sem passar por
        # nós; para envolvê-lo no gate, montamos e servimos aqui.
        import uvicorn

        uvicorn.run(
            GateDoProxy(mcp.streamable_http_app(), PROXY_SEGREDO, HEADER_SEGREDO),
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MCP_PORT", "8000")),
        )
    else:
        mcp.run()  # stdio (padrão)
