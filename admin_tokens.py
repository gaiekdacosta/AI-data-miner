#!/usr/bin/env python3
"""Gestão dos tokens de acesso ao MCP — a camada de dados do painel.

Um "usuário" aqui É um bloco do `caddy/tokens.caddy`: um matcher com o Bearer da
pessoa e um `handle` que encaminha para o `mcp`. Criar / rotacionar / revogar é
editar esse arquivo e **recarregar o Caddy**, sem derrubar as sessões de ninguém.

Decisões que não são óbvias (mexer nelas quebra em silêncio):

* **Escrita IN PLACE.** O proxy monta o `tokens.caddy` como bind-mount de arquivo
  único. Gravar por `os.replace`/rename trocaria o inode e o container do Caddy
  continuaria lendo o conteúdo ANTIGO — o token novo simplesmente não valeria.
* **Reload pelo socket unix de admin**, não por porta. O `admin` do Caddy dá
  controle total sobre a configuração; num socket compartilhado só por volume, o
  `mcp` (e qualquer outro container da rede) não alcança.
* **O corpo enviado no reload é o Caddyfile**, adaptado DENTRO do container do
  proxy. Por isso o `import` dele tem de ser ABSOLUTO (/etc/caddy/tokens.caddy):
  como não mandamos um caminho de arquivo, um import relativo seria resolvido
  contra o cwd do processo do Caddy, não contra o Caddyfile.
* **Aplicar é transacional:** grava, recarrega, e se o Caddy recusar a
  configuração volta o conteúdo anterior e recarrega de novo. Nunca deixa o
  gate num estado que o Caddy não aceita.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import secrets
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

import perfis as _perfis

TOKENS_PATH = os.environ.get("TOKENS_PATH", "/caddy/tokens.caddy")
CADDYFILE_PATH = os.environ.get("CADDYFILE_PATH", "/caddy/Caddyfile")
ADMIN_SOCK = os.environ.get("CADDY_ADMIN_SOCK", "/run/caddy/admin.sock")
ACCESS_LOG = os.environ.get("CADDY_ACCESS_LOG", "/caddy/log/access.log")
UPSTREAM = os.environ.get("MCP_UPSTREAM", "mcp:8000")
HEADER_PERFIS = os.environ.get("MCP_HEADER_PERFIS", "X-Db-Perfis")
# Identidade da pessoa para a trilha de auditoria do mcp_server (quem consultou o
# quê). Mesmo mecanismo dos perfis: header_up é SET, o cliente não forja.
HEADER_PESSOA = os.environ.get("MCP_HEADER_PESSOA", "X-Db-Pessoa")
# Vencimento do acesso, no mesmo mecanismo dos perfis: o bloco da pessoa injeta a
# data e o mcp_server recusa consulta depois dela. Token que não vence é token que
# um vazamento antigo (config copiada para a máquina errada, um print de tela)
# mantém válido para sempre — e aqui ele lê o banco.
HEADER_EXPIRA = os.environ.get("MCP_HEADER_EXPIRA", "X-Db-Expira")
# Prazo de um acesso novo, e de um token rotacionado (rotacionar é emitir
# credencial nova, então renova o prazo junto).
PRAZO_DIAS = int(os.environ.get("TOKEN_PRAZO_DIAS", "90"))
# A partir de quantos dias para o fim o painel avisa.
AVISO_DIAS = int(os.environ.get("TOKEN_AVISO_DIAS", "15"))

# Bancos que se pode liberar por pessoa: (chave, rótulo, descrição, sensível).
# Vêm do .env (PERFIS, PERFIS_SENSIVEIS, PERFIS_INICIAIS) — o mesmo registro que o
# mcp_server usa, então a tela nunca oferece um banco que o servidor não conhece.
# `sensivel` marca o que não é liberado por padrão (dado de pessoa).
PERFIS = tuple(
    (p, _perfis.rotulo(p), _perfis.descricao(p), _perfis.sensivel(p))
    for p in _perfis.PERFIS
)
PERFIS_VALIDOS = tuple(p[0] for p in PERFIS)
# Novo acesso começa com o menor privilégio: liberar banco sensível é decisão
# explícita de quem administra.
PERFIS_INICIAIS = _perfis.INICIAIS

# Linha do matcher de uma pessoa: @rotulo header Authorization "Bearer <token>"
_MATCHER = re.compile(
    r'@(?P<label>[A-Za-z0-9_.-]+)\s+header\s+Authorization\s+"Bearer\s+(?P<token>[^"]+)"'
)
# Linha que carrega os bancos liberados da pessoa para o servidor MCP.
_HEADER_LINHA = re.compile(
    r'^\s*header_up\s+' + re.escape(HEADER_PERFIS) + r'\s+"(?P<perfis>[^"]*)"\s*$'
)
# Linha que carrega o vencimento do acesso. É a ÚNICA representação da data (não
# há cópia no comentário de metadados): duas fontes divergiriam no primeiro
# bloco editado à mão, e a que vale é esta, porque é a que o mcp recebe.
_EXPIRA_LINHA = re.compile(
    r'^\s*header_up\s+' + re.escape(HEADER_EXPIRA) + r'\s+"(?P<data>[^"]*)"\s*$'
)
# Metadados que o painel grava no comentário do bloco (o Caddy ignora).
_META = re.compile(r'(?P<chave>nome|criado)="(?P<valor>[^"]*)"')
# Marca a linha de metadados de um bloco escrito pelo painel.
_MARCADOR = "motor-consulta-token"


class ErroDeToken(Exception):
    """Erro previsto, com mensagem pronta para mostrar na tela."""


@dataclass
class Pessoa:
    label: str
    token: str
    nome: str = ""
    criado: str = ""
    # Bancos liberados. Vazio = o bloco não declara nada; o mcp então cai no
    # MCP_PERFIS_PADRAO, que no compose é vazio (nega tudo).
    perfis: tuple = ()
    # Vencimento (YYYY-MM-DD). Vazio = bloco sem a linha, ou seja, acesso que
    # não vence — é o estado legado, e o painel o mostra como pendência.
    expira: str = ""
    # Faixa de linhas que o bloco ocupa no arquivo (comentários inclusive).
    inicio: int = 0
    fim: int = 0
    # Preenchido por `com_uso()`.
    ultimo_uso: str = ""
    chamadas: int = 0

    @property
    def exibicao(self) -> str:
        return self.nome or self.label

    @property
    def dias_restantes(self):
        """Dias até vencer (0 = vence hoje, negativo = venceu). None = sem prazo."""
        if not self.expira:
            return None
        try:
            fim = datetime.strptime(self.expira[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
        return (fim - datetime.now().date()).days

    @property
    def vencido(self) -> bool:
        d = self.dias_restantes
        return d is not None and d < 0


# --------------------------------------------------------------------- leitura

def ler_texto() -> str:
    with open(TOKENS_PATH, encoding="utf-8") as f:
        return f.read()


def _fim_do_handle(linhas: list[str], i_handle: int) -> int:
    """Índice da linha que fecha o `handle {` aberto em i_handle."""
    profundidade = 0
    for i in range(i_handle, len(linhas)):
        profundidade += linhas[i].count("{") - linhas[i].count("}")
        if profundidade <= 0:
            return i
    raise ErroDeToken(
        f"o bloco que começa na linha {i_handle + 1} do tokens.caddy não fecha — "
        "corrija o arquivo à mão antes de usar o painel"
    )


def listar(texto: str | None = None) -> list[Pessoa]:
    """Lê o tokens.caddy e devolve uma Pessoa por bloco, na ordem do arquivo."""
    linhas = (texto if texto is not None else ler_texto()).splitlines()
    pessoas: list[Pessoa] = []
    i = 0
    while i < len(linhas):
        m = _MATCHER.search(linhas[i])
        if not m or linhas[i].lstrip().startswith("#"):
            i += 1
            continue
        label = m.group("label")

        # O handle da pessoa vem depois do matcher.
        i_handle = next(
            (j for j in range(i + 1, len(linhas)) if f"handle @{label}" in linhas[j]),
            None,
        )
        if i_handle is None:
            raise ErroDeToken(f"o matcher @{label} não tem um `handle @{label}` depois dele")
        i_fim = _fim_do_handle(linhas, i_handle)

        # Comentários imediatamente acima pertencem ao bloco (param em linha vazia).
        inicio = i
        j = i - 1
        while j >= 0 and linhas[j].lstrip().startswith("#"):
            inicio = j
            j -= 1

        expira = ""
        for linha in linhas[i_handle : i_fim + 1]:
            me = _EXPIRA_LINHA.match(linha)
            if me:
                expira = me.group("data").strip()
                break

        perfis: tuple = ()
        for linha in linhas[i_handle : i_fim + 1]:
            mh = _HEADER_LINHA.match(linha)
            if mh:
                bruto = mh.group("perfis").strip()
                perfis = (
                    PERFIS_VALIDOS if bruto == "*"
                    else tuple(
                        x for x in PERFIS_VALIDOS
                        if x in {t.strip().lower() for t in bruto.split(",")}
                    )
                )
                break

        meta = dict(
            (mm.group("chave"), mm.group("valor"))
            for linha in linhas[inicio:i]
            for mm in _META.finditer(linha)
        )
        pessoas.append(
            Pessoa(
                label=label,
                token=m.group("token"),
                nome=meta.get("nome", ""),
                criado=meta.get("criado", ""),
                perfis=perfis,
                expira=expira,
                inicio=inicio,
                fim=i_fim,
            )
        )
        i = i_fim + 1
    return pessoas


# ------------------------------------------------------------------- gravação

def _backups() -> list:
    pasta = os.path.dirname(TOKENS_PATH) or "."
    base = os.path.basename(TOKENS_PATH) + ".bak-"
    try:
        return sorted(f for f in os.listdir(pasta) if f.startswith(base))
    except OSError:
        return []


# Quem está agindo nesta requisição, preenchido pelo admin_app a cada POST (forma
# de entrada + IP de origem). Com o painel na rede, "quem revogou" tem mais de uma
# resposta possível. ContextVar, e não global: uvicorn atende requisições
# concorrentes e uma global misturaria os operadores.
OPERADOR: contextvars.ContextVar[str] = contextvars.ContextVar("operador", default="?")


def _registrar_escrita(antes: int, depois: int) -> None:
    """Deixa rastro de QUEM escreveu no arquivo de tokens.

    Existe porque "o arquivo de tokens amanheceu vazio" não tem resposta sem
    isto: o access_log do painel diz que houve um POST, não o que ele gravou.
    Vai para o stderr (docker compose logs admin) e para
    auditoria-tokens/tokens-AAAA-MM-DD.jsonl.
    """
    import json
    import traceback
    pilha = [l.strip().replace("\n", " ")
             for l in traceback.format_stack()[:-2]
             if "admin_tokens.py" in l or "admin_app.py" in l][-4:]
    operador = OPERADOR.get()
    registro = {"ts": datetime.now().isoformat(timespec="seconds"),
                "operador": operador,
                "acessos_antes": antes, "acessos_depois": depois, "pilha": pilha}
    print(f"[tokens] escrita por {operador}: {antes} -> {depois} acessos "
          f"| {' <- '.join(pilha)}", file=sys.stderr, flush=True)
    try:
        # Diretório PRÓPRIO, e não o da trilha de consultas: o painel monta aquele
        # como read-only de propósito — quem é auditado não reescreve a auditoria.
        dir_aud = os.environ.get("TOKENS_AUDIT_DIR", "/auditoria-tokens")
        os.makedirs(dir_aud, mode=0o750, exist_ok=True)
        caminho = os.path.join(dir_aud, f"tokens-{datetime.now():%Y-%m-%d}.jsonl")
        novo = not os.path.exists(caminho)
        with open(caminho, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
        if novo:
            os.chmod(caminho, 0o600)
    except Exception:
        pass


def _gravar(texto: str, guardar=10) -> None:
    """Grava o tokens.caddy IN PLACE (ver docstring do módulo: bind-mount).

    Backup COM DATA, não um `.bak` único: o rollback do `_aplicar` também grava, e
    com nome fixo ele sobrescrevia justamente a cópia boa — o backup evaporava na
    hora em que era necessário. Mantém os `guardar` mais recentes.
    """
    if os.path.exists(TOKENS_PATH):
        bak = f"{TOKENS_PATH}.bak-{datetime.now():%Y%m%d-%H%M%S-%f}"[:-3]
        shutil.copy2(TOKENS_PATH, bak)
        os.chmod(bak, 0o600)
        pasta = os.path.dirname(TOKENS_PATH) or "."
        for antigo_bak in _backups()[:-guardar]:
            try:
                os.remove(os.path.join(pasta, antigo_bak))
            except OSError:
                pass
    try:
        _registrar_escrita(len(listar(ler_texto())), len(listar(texto)))
    except Exception:
        pass
    modo = "r+" if os.path.exists(TOKENS_PATH) else "w"
    with open(TOKENS_PATH, modo, encoding="utf-8") as f:
        f.write(texto)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())
    os.chmod(TOKENS_PATH, 0o600)


def recarregar() -> tuple[bool, str]:
    """Recarrega o Caddy pelo socket de admin. (ok, mensagem de erro)."""
    try:
        with open(CADDYFILE_PATH, "rb") as f:
            corpo = f.read()
    except OSError as e:
        return False, f"não consegui ler o Caddyfile ({CADDYFILE_PATH}): {e}"
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=ADMIN_SOCK), timeout=30) as c:
            r = c.post(
                "http://localhost/load",
                content=corpo,
                headers={"Content-Type": "text/caddyfile"},
            )
    except Exception as e:  # socket ausente = proxy fora do ar ou volume errado
        return False, (
            f"não alcancei o admin do Caddy em {ADMIN_SOCK}: {e}. "
            "O proxy está no ar? O volume do socket está montado nos dois containers?"
        )
    if r.status_code == 200:
        return True, ""
    detalhe = r.text.strip()
    try:
        detalhe = json.loads(detalhe).get("error", detalhe)
    except Exception:
        pass
    return False, f"o Caddy recusou a configuração (HTTP {r.status_code}): {detalhe}"


def _aplicar(novo: str, delta: int = 0) -> None:
    """Grava e recarrega; se o Caddy recusar, desfaz e recarrega de volta.

    `delta` é quantos acessos a operação PODE mudar: +1 criar, -1 revogar, 0 para
    trocar perfis/rotacionar. Se a contagem não fechar, nada é gravado — é a trava
    que impede um bug (ou uma requisição repetida/concorrente) de esvaziar o
    arquivo de tokens e derrubar o gate de todo mundo.
    """
    anterior = ler_texto()
    antes, depois = len(listar(anterior)), len(listar(novo))
    if depois - antes != delta:
        raise ErroDeToken(
            f"operação recusada: mudaria de {antes} para {depois} acessos "
            f"(esperado {antes + delta:+d} → {antes + delta}). Nada foi gravado; "
            f"o arquivo está intacto e há backups em {os.path.basename(TOKENS_PATH)}.bak-*"
        )
    _gravar(novo)
    ok, erro = recarregar()
    if ok:
        return
    _gravar(anterior)
    ok2, erro2 = recarregar()
    extra = "" if ok2 else f" ATENÇÃO: o rollback também falhou ({erro2})."
    raise ErroDeToken(f"nada foi alterado. {erro}{extra}")


# ---------------------------------------------------------------------- ações

def rotulo_de(nome: str) -> str:
    """Nome da pessoa -> rótulo do Caddy (o @ que se apaga para revogar)."""
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", nome) if not unicodedata.combining(c)
    )
    label = re.sub(r"[^A-Za-z0-9]+", "_", sem_acento).strip("_").lower()
    if not label:
        raise ErroDeToken("informe um nome com pelo menos uma letra ou número")
    return label[:48]


def novo_token() -> str:
    """64 hex — o mesmo formato de `openssl rand -hex 32`."""
    return secrets.token_hex(32)


def _normalizar_perfis(perfis) -> tuple:
    """Só perfis conhecidos, na ordem canônica (a da tela)."""
    pedidos = {str(x).strip().lower() for x in (perfis or [])}
    return tuple(x for x in PERFIS_VALIDOS if x in pedidos)


def _linha_perfis(perfis) -> str:
    """A linha que diz ao mcp quais bancos esta pessoa alcança.

    `header_up <campo> <valor>` é SET: sobrescreve o que o cliente tenha mandado.
    Sem esta linha, o mcp cai no MCP_PERFIS_PADRAO (vazio no compose = nega tudo).
    """
    return f'\t\theader_up {HEADER_PERFIS} "{",".join(perfis)}"'


def _linha_pessoa(label: str) -> str:
    return f'\t\theader_up {HEADER_PESSOA} "{label}"'


def _linha_expira(data: str) -> str:
    """A linha que diz ao mcp até quando este acesso vale (inclusive)."""
    return f'\t\theader_up {HEADER_EXPIRA} "{data}"'


def daqui(dias=None) -> str:
    """Data de vencimento a partir de hoje, em YYYY-MM-DD."""
    return (datetime.now() + timedelta(days=PRAZO_DIAS if dias is None else int(dias))
            ).strftime("%Y-%m-%d")


def _ajustar_linha(texto: str, label: str, regex, nova: str) -> str:
    """Texto com UMA linha do bloco da pessoa trocada (ou inserida). Não grava.

    Serve para as duas linhas que o painel administra dentro do `reverse_proxy`
    (perfis e vencimento): a lógica de "onde inserir quando o bloco foi escrito à
    mão e não tem a linha" é a mesma, e estava só no `definir_perfis`.
    """
    alvo = next((p for p in listar(texto) if p.label == label), None)
    if alvo is None:
        raise ErroDeToken(f"não achei um acesso com o rótulo @{label}")
    linhas = texto.splitlines()
    i_existente = next(
        (i for i in range(alvo.inicio, alvo.fim + 1) if regex.match(linhas[i])), None)
    if i_existente is not None:
        linhas[i_existente] = nova
    else:
        # Bloco escrito à mão (sem a linha): entra logo depois do
        # `header_up -Authorization`, dentro do reverse_proxy — se não houver,
        # depois da abertura do reverse_proxy.
        destino = next(
            (i for i in range(alvo.inicio, alvo.fim + 1)
             if "header_up -Authorization" in linhas[i]), None)
        if destino is None:
            destino = next(
                (i for i in range(alvo.inicio, alvo.fim + 1) if "reverse_proxy" in linhas[i]),
                None)
            if destino is None:
                raise ErroDeToken(
                    f"o bloco @{label} não tem um reverse_proxy — ajuste à mão")
        linhas.insert(destino + 1, nova)
    return "\n".join(linhas).rstrip("\n") + "\n"


def _bloco(label: str, token: str, nome: str, criado: str, perfis, expira="") -> list[str]:
    nome_limpo = nome.replace('"', "").strip()
    return [
        "",
        f'# {_MARCADOR} nome="{nome_limpo}" criado="{criado}"',
        f'@{label} header Authorization "Bearer {token}"',
        f"handle @{label} {{",
        # Atribui a chamada à pessoa no log de acesso — é o que o painel usa para
        # mostrar "último uso" e decidir quem já não usa (e pode ser revogado).
        f"\tlog_append pessoa {label}",
        f"\treverse_proxy {UPSTREAM} {{",
        "\t\theader_up -Authorization",
        _linha_pessoa(label),
        _linha_perfis(perfis),
        _linha_expira(expira or daqui()),
        "\t\tflush_interval -1",
        "\t}",
        "}",
    ]


def _achatar(v: str) -> str:
    """Forma comparável de um nome: sem acento, sem pontuação, minúsculo."""
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", v) if not unicodedata.combining(c)
    )
    return re.sub(r"[^a-z0-9]+", "", sem_acento.lower())


def criar(nome: str, perfis=PERFIS_INICIAIS) -> tuple[str, str]:
    """Cria a pessoa e devolve (label, token). Levanta ErroDeToken se já existe."""
    label = rotulo_de(nome)
    texto = ler_texto()
    existentes = listar(texto)
    # Duas checagens, e as duas importam: o rótulo pode coincidir, mas o rótulo de
    # um bloco escrito à mão (@maria) não bate com o derivado do nome
    # (@maria_silva) — sem comparar o NOME, a pessoa ganharia um segundo
    # acesso paralelo, que ninguém lembraria de revogar.
    ja = next(
        (
            p for p in existentes
            if p.label == label
            or (p.nome and _achatar(p.nome) == _achatar(nome))
            or _achatar(p.label) == _achatar(nome)
        ),
        None,
    )
    if ja is not None:
        raise ErroDeToken(
            f"{ja.exibicao} já tem acesso (rótulo @{ja.label}). "
            "Use 'rotacionar' para trocar o token dela, ou 'revogar' antes de recriar."
        )
    token = novo_token()
    criado = datetime.now().strftime("%Y-%m-%d %H:%M")
    linhas = texto.rstrip("\n").splitlines() + _bloco(
        label, token, nome, criado, _normalizar_perfis(perfis), daqui()
    )
    _aplicar("\n".join(linhas) + "\n", delta=+1)
    return label, token


def revogar(label: str) -> str:
    """Apaga o bloco da pessoa. Devolve o nome de exibição."""
    texto = ler_texto()
    pessoas = listar(texto)
    alvo = next((p for p in pessoas if p.label == label), None)
    if alvo is None:
        raise ErroDeToken(f"não achei um acesso com o rótulo @{label}")
    linhas = texto.splitlines()
    del linhas[alvo.inicio : alvo.fim + 1]
    # Não deixa duas linhas vazias seguidas no lugar do bloco removido.
    limpas: list[str] = []
    for linha in linhas:
        if not linha.strip() and limpas and not limpas[-1].strip():
            continue
        limpas.append(linha)
    _aplicar("\n".join(limpas).rstrip("\n") + "\n", delta=-1)
    return alvo.exibicao


def definir_perfis(label: str, perfis) -> tuple[str, tuple]:
    """Troca os bancos liberados da pessoa. Devolve (exibicao, perfis aplicados).

    Vale na próxima chamada dela: o header é resolvido a cada requisição, então
    não é preciso pedir para ninguém reconectar.
    """
    perfis = _normalizar_perfis(perfis)
    texto = ler_texto()
    alvo = next((p for p in listar(texto) if p.label == label), None)
    if alvo is None:
        raise ErroDeToken(f"não achei um acesso com o rótulo @{label}")
    if tuple(alvo.perfis) == perfis:
        return alvo.exibicao, perfis  # nada a fazer: não mexe no arquivo

    _aplicar(_ajustar_linha(texto, label, _HEADER_LINHA, _linha_perfis(perfis)))
    return alvo.exibicao, perfis


def rotacionar(label: str) -> tuple[str, str]:
    """Troca o token da pessoa mantendo o rótulo. Devolve (exibicao, token novo).

    O token antigo para de valer no mesmo instante — a pessoa precisa reconfigurar.
    """
    texto = ler_texto()
    alvo = next((p for p in listar(texto) if p.label == label), None)
    if alvo is None:
        raise ErroDeToken(f"não achei um acesso com o rótulo @{label}")
    token = novo_token()
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    meta = f'# {_MARCADOR} nome="{alvo.nome or alvo.label}" criado="{agora}"'
    linhas = texto.splitlines()
    tinha_meta = False
    i_matcher = alvo.inicio
    for i in range(alvo.inicio, alvo.fim + 1):
        if _MATCHER.search(linhas[i]) and not linhas[i].lstrip().startswith("#"):
            linhas[i] = f'@{label} header Authorization "Bearer {token}"'
            i_matcher = i
        elif _MARCADOR in linhas[i]:
            linhas[i] = meta
            tinha_meta = True
    # Bloco escrito à mão não tem a linha de metadados; cria agora, para o painel
    # passar a mostrar quando o token foi gerado.
    if not tinha_meta:
        linhas.insert(i_matcher, meta)
    # Rotacionar é EMITIR credencial nova, então o prazo recomeça. Aplicado sobre
    # o texto já com o token novo (e por _ajustar_linha, que relista o bloco: os
    # índices acima podem ter andado com o insert da linha de metadados).
    texto_novo = "\n".join(linhas).rstrip("\n") + "\n"
    _aplicar(_ajustar_linha(texto_novo, label, _EXPIRA_LINHA, _linha_expira(daqui())))
    return alvo.exibicao, token


def renovar(label: str, dias=None) -> tuple[str, str]:
    """Estende o prazo SEM trocar o token. Devolve (exibicao, nova data).

    É a ação do dia a dia: o token da pessoa continua valendo (ninguém precisa
    reconfigurar máquina), só o vencimento anda para frente. Trocar o token é
    `rotacionar`, e serve para outra coisa — suspeita de vazamento.
    """
    texto = ler_texto()
    alvo = next((p for p in listar(texto) if p.label == label), None)
    if alvo is None:
        raise ErroDeToken(f"não achei um acesso com o rótulo @{label}")
    data = daqui(dias)
    _aplicar(_ajustar_linha(texto, label, _EXPIRA_LINHA, _linha_expira(data)))
    return alvo.exibicao, data


# ------------------------------------------------------------------------ uso

@dataclass
class Uso:
    por_pessoa: dict[str, dict] = field(default_factory=dict)
    recusadas: int = 0          # 401 do proxy: token errado ou revogado
    disponivel: bool = False    # o log de acesso existe e foi lido?


def uso(dias: int = 7, max_bytes: int = 2_000_000) -> Uso:
    """Agrega o log de acesso do Caddy (JSON) por pessoa.

    Lê só o final do arquivo: o log rotaciona em 10MiB e o painel não precisa de
    histórico completo — precisa de "quem usou por último". Falha em silêncio
    (disponivel=False) se o log ainda não existe.
    """
    resultado = Uso()
    try:
        tamanho = os.path.getsize(ACCESS_LOG)
        with open(ACCESS_LOG, "rb") as f:
            if tamanho > max_bytes:
                f.seek(tamanho - max_bytes)
                f.readline()  # descarta a linha partida
            bruto = f.read().decode("utf-8", "replace")
    except OSError:
        return resultado

    resultado.disponivel = True
    corte = time.time() - dias * 86400
    for linha in bruto.splitlines():
        if not linha.startswith("{"):
            continue
        try:
            reg = json.loads(linha)
        except ValueError:
            continue
        ts = reg.get("ts") or 0
        if ts < corte:
            continue
        pessoa = reg.get("pessoa")
        if not pessoa:
            if reg.get("status") == 401:
                resultado.recusadas += 1
            continue
        d = resultado.por_pessoa.setdefault(pessoa, {"chamadas": 0, "ultimo": 0.0})
        d["chamadas"] += 1
        d["ultimo"] = max(d["ultimo"], ts)
    return resultado


def _humano(ts: float) -> str:
    if not ts:
        return ""
    delta = time.time() - ts
    if delta < 90:
        return "agora"
    if delta < 3600:
        return f"{int(delta // 60)} min"
    if delta < 86400:
        return f"{int(delta // 3600)} h"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)} d"
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")


def com_uso(dias: int = 7) -> tuple[list[Pessoa], Uso]:
    """Pessoas do tokens.caddy já anotadas com o uso recente."""
    pessoas = listar()
    u = uso(dias)
    for p in pessoas:
        d = u.por_pessoa.get(p.label)
        if d:
            p.chamadas = d["chamadas"]
            p.ultimo_uso = _humano(d["ultimo"])
    return pessoas, u
