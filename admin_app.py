#!/usr/bin/env python3
"""Painel de acessos do MCP — administra as pessoas e os tokens.

Uma tela só: lista quem tem acesso, gera token novo, rotaciona, renova, revoga e
testa o token contra o proxy de verdade. A escrita e o reload do Caddy ficam em
`admin_tokens.py` (leia a docstring dele: as armadilhas estão lá).

Sobe com:
    docker compose up -d admin        # publicado em 127.0.0.1:8012 por padrão

Sem ``ADMIN_SENHA`` ele SÓ aceita subir publicado em loopback (``ADMIN_BIND``):
a tela cria credencial de acesso aos bancos, então abrir na rede sem login não é
uma opção que este arquivo oferece. Com senha, pode ir para a rede interna — de
preferência atrás de HTTPS.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from datetime import datetime
import html
import json
import os
import secrets
import sys
import time

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

import admin_tokens as T
import auditoria
import perguntas

PORTA = int(os.environ.get("ADMIN_PORT", "8012"))
BIND = os.environ.get("ADMIN_BIND", "127.0.0.1")
SENHA = os.environ.get("ADMIN_SENHA", "").strip()
NOME_SERVIDOR = os.environ.get("MCP_NOME", "motor-consulta")
# Só para montar os comandos prontos que a tela mostra (não muda o gate).
ENDPOINT = os.environ.get("MCP_ENDPOINT", "http://localhost:8011/mcp")
PROXY_INTERNO = os.environ.get("MCP_PROXY_INTERNO", "http://proxy:8011/mcp")
# Sub-rede do compose: o host chega à porta publicada pelo GATEWAY dela, não como
# 127.0.0.1. Serve só para liberar o TEXTO do /saude ao watchdog (ver _local).
REDE_SUBNET = os.environ.get("REDE_SUBNET", "").strip()
DIAS = int(os.environ.get("ADMIN_DIAS_USO", "7"))
# Janela do catálogo de perguntas — maior que a da trilha de propósito:
# recorrência de pergunta só aparece no mês (no dia, tudo parece pergunta nova).
DIAS_PERGUNTAS = int(os.environ.get("ADMIN_DIAS_PERGUNTAS", "30"))
# Onde o gerar_dossie.sh deixa o relatório de lacunas. O painel só LÊ (o volume é
# :ro): quem gera é o mcp, único serviço com contexto/ montado.
DOSSIE_DIR = os.environ.get("DOSSIE_DIR", "dossie")

# Chave de assinatura por boot: reiniciar o painel derruba as sessões abertas.
_CHAVE = secrets.token_bytes(32)
_CSRF = hmac.new(_CHAVE, b"csrf", hashlib.sha256).hexdigest()
COOKIE = "motor_consulta_admin"
# Resultado de uma ação, entregue UMA vez na próxima tela. Não vai na URL: o
# token novo apareceria no histórico do navegador e nos logs.
_RESULTADOS: dict[str, dict] = {}


def _guardar(**dados) -> str:
    chave = secrets.token_urlsafe(8)
    _RESULTADOS[chave] = dados
    # Não deixa crescer: isto é uma tela de uso pontual.
    for antiga in list(_RESULTADOS)[:-20]:
        _RESULTADOS.pop(antiga, None)
    return chave


# ------------------------------------------------------------------- sessão

def _assinar(exp: int) -> str:
    mac = hmac.new(_CHAVE, str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def _valido(valor: str | None) -> bool:
    if not valor or "." not in valor:
        return False
    exp, _, mac = valor.partition(".")
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(_assinar(int(exp)), valor)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _operador(request: Request) -> str:
    """Quem está agindo, para a trilha de tokens: a forma de entrada + o IP.

    A senha do painel é compartilhada, então o IP é o que sobra para distinguir
    duas pessoas que administram — por isso ele vai junto.
    """
    via = "senha do painel" if SENHA else "loopback sem login"
    return f"{via} ({_ip(request)})"


def _gateway_da_rede():
    try:
        rede = ipaddress.ip_network(REDE_SUBNET, strict=False)
        return str(rede.network_address + 1)
    except ValueError:
        return None


_GATEWAY = _gateway_da_rede()


def _local(request: Request) -> bool:
    """Chamador de infraestrutura: o healthcheck (127.0.0.1, dentro do container)
    e o watchdog no host, que bate na porta publicada e chega pelo GATEWAY da rede
    do compose — não como 127.0.0.1. Vale só para liberar o TEXTO do /saude; não
    autentica nada nem abre outra rota."""
    ip = _ip(request)
    return ip in ("127.0.0.1", "::1") or (_GATEWAY is not None and ip == _GATEWAY)


def _autenticado(request: Request) -> bool:
    if SENHA:
        return _valido(request.cookies.get(COOKIE))
    # Sem senha o painel é aberto — e por isso o boot só o deixa subir publicado
    # em loopback (ver __main__).
    return True


def _csrf_ok(form) -> bool:
    return hmac.compare_digest(str(form.get("csrf", "")), _CSRF)


# --------------------------------------------------------------------- HTML

CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--txt:#1a1d21;--sub:#5b6470;
--linha:#e3e6ea;--ok:#0a7d4b;--okbg:#e8f6ee;--erro:#b3261e;--errobg:#fdecea;
--acao:#1a56db;--aviso:#8a5a00;--avisobg:#fff6e0;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--card:#1c2024;--txt:#e7eaee;
--sub:#9aa4b1;--linha:#2b3238;--ok:#4ade80;--okbg:#123125;--erro:#ff8a80;--errobg:#3a1c1a;
--acao:#7aa2ff;--aviso:#ffcf70;--avisobg:#3a2f14}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:28px 20px 60px}
/* Telas de DADOS (home, trilha, perguntas, dossiê) trabalham largas; as de
   leitura e os forms pequenos (comandos, login, confirmação) ficam em 1040. */
.wrap.largo{max-width:1560px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 12px}
.sub{color:var(--sub);font-size:13px;margin:0 0 22px}
.card{background:var(--card);border:1px solid var(--linha);border-radius:12px;padding:18px;margin-bottom:18px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--linha);vertical-align:middle}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--sub)}
tr:last-child td{border-bottom:0}
.mono{font-family:var(--mono);font-size:12.5px}
.tok{display:inline-flex;align-items:center;gap:6px}
.tok code{background:rgba(127,127,127,.12);padding:2px 6px;border-radius:6px;word-break:break-all}
button,input[type=submit]{font:inherit;border-radius:8px;border:1px solid var(--linha);
background:var(--card);color:var(--txt);padding:6px 12px;cursor:pointer}
button:hover{border-color:var(--acao);color:var(--acao)}
button.primario{background:var(--acao);border-color:var(--acao);color:#fff;padding:8px 16px}
button.primario:hover{opacity:.9;color:#fff}
button.perigo:hover{border-color:var(--erro);color:var(--erro)}
input[type=text],input[type=password]{font:inherit;padding:8px 10px;border-radius:8px;
border:1px solid var(--linha);background:var(--bg);color:var(--txt);min-width:260px}
form.linha{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.acoes{display:flex;gap:6px;justify-content:flex-end}
.ok{background:var(--okbg);border-color:var(--ok)}
.erro{background:var(--errobg);border-color:var(--erro)}
.aviso{background:var(--avisobg);border-color:var(--aviso)}
.rot{color:var(--sub);font-size:12px}
pre{background:rgba(127,127,127,.12);padding:10px 12px;border-radius:8px;overflow-x:auto;
font-family:var(--mono);font-size:12.5px;margin:8px 0}
.nada{color:var(--sub);font-style:italic}
.bancos{display:flex;gap:4px;flex-wrap:wrap;align-items:center}
.bancos label{display:inline-flex;align-items:center;gap:4px;font-size:12.5px;
 border:1px solid var(--linha);border-radius:999px;padding:2px 9px 2px 6px;cursor:pointer;
 background:var(--bg);white-space:nowrap}
.bancos label:hover{border-color:var(--acao)}
.bancos label.sens{border-style:dashed}
.prazo{font-size:11.5px;border:1px solid var(--linha);border-radius:999px;
 padding:1px 8px;white-space:nowrap;color:var(--sub)}
.prazo.venceu{border-color:var(--erro);background:var(--errobg);color:var(--erro);font-weight:600}
.prazo.perto{border-color:var(--aviso);background:var(--avisobg);color:var(--aviso);font-weight:600}
.prazo.sem{border-style:dashed;border-color:var(--aviso);color:var(--aviso)}
.bancos input:checked+span{font-weight:600;color:var(--acao)}
.bancos label.sens input:checked+span{color:var(--aviso)}
.bancos button{padding:3px 9px;font-size:12.5px}
.sql{font-family:var(--mono);font-size:12px;white-space:pre-wrap;word-break:break-word;
 max-height:4.5em;overflow:hidden}
.neg{color:var(--erro);font-weight:600}
.filtros{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.filtros a{text-decoration:none;border:1px solid var(--linha);border-radius:999px;
 padding:3px 11px;color:var(--txt);font-size:12.5px}
.filtros a:hover,.filtros a.on{border-color:var(--acao);color:var(--acao)}
.campo{display:flex;gap:8px;align-items:center;margin:6px 0 2px}
.campo input{flex:1;min-width:0;font-family:var(--mono);font-size:12.5px}
.link-topo{float:right;text-decoration:none;border:1px solid var(--linha);border-radius:8px;
 padding:6px 12px;color:var(--txt);font-size:13px}
.link-topo:hover{border-color:var(--acao);color:var(--acao)}
.tag{display:inline-block;font-size:11.5px;border:1px solid var(--linha);
 border-radius:999px;padding:1px 8px;color:var(--sub);white-space:nowrap}
.tag.alerta{border-color:var(--aviso);color:var(--aviso)}
details summary{cursor:pointer;font-size:12.5px;color:var(--sub)}
details[open] summary{margin-bottom:6px}
.perg{font-size:14px}
footer{color:var(--sub);font-size:12.5px;margin-top:26px}
footer code{font-family:var(--mono)}
"""

# Estilos só da tela /dossie. Fora do CSS comum de propósito: são ~40 regras que
# nenhuma outra página usa, e todas as cores saem das variáveis do tema — a tela
# tem de funcionar no modo escuro como o resto do painel.
CSS_DOSSIE = """
.acao-topo{margin:0 0 18px}
.numeros{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;
 padding:16px 18px}
.numeros div{display:flex;flex-direction:column;gap:2px}
.numeros b{font-size:26px;line-height:1.1;font-variant-numeric:tabular-nums}
.numeros span{color:var(--sub);font-size:12px}
.triagem{padding:6px 8px}
.triagem .item{display:grid;grid-template-columns:26px 1fr 120px auto 62px;gap:10px;
 align-items:center;padding:9px 10px;border-radius:8px;text-decoration:none;color:var(--txt)}
.triagem .item:hover{background:rgba(127,127,127,.09)}
.triagem .item+.item{border-top:1px solid var(--linha)}
.triagem .txt{font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.triagem .sql-n{text-align:right;white-space:nowrap}
.num{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;
 border-radius:6px;background:rgba(127,127,127,.14);color:var(--sub);
 font-size:12px;font-weight:600;flex:none}
.medidor{position:relative;height:7px;border-radius:999px;background:rgba(127,127,127,.16);
 min-width:90px}
.medidor .barra{height:100%;background:var(--aviso);border-radius:999px}
.medidor .barra.vazio{background:var(--erro);opacity:.55}
/* O traço do piso passa de fora a fora da barra: sem `overflow:visible` no pai
   ele era cortado na altura dos 7px e virava um pontinho. */
.medidor .piso{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--sub);
 opacity:.7;border-radius:1px}
.lacuna{padding:20px 22px}
.topo-lacuna{display:flex;gap:10px;align-items:flex-start;margin-bottom:12px}
.topo-lacuna h2{font-size:16px;line-height:1.35;margin:0}
.grav{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.grav .medidor{width:150px;min-width:150px}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px 18px;
 padding:12px 0;border-top:1px solid var(--linha);border-bottom:1px solid var(--linha);
 font-size:13px}
.meta>div{display:flex;flex-wrap:wrap;gap:5px;align-items:baseline}
.chip{display:inline-block;font-size:11.5px;border-radius:999px;padding:1px 8px;
 background:rgba(127,127,127,.14);color:var(--txt)}
.ofereceu{margin:10px 0 14px}
.ofereceu code{background:rgba(127,127,127,.12);padding:1px 5px;border-radius:5px;
 font-family:var(--mono);font-size:11.5px}
.rascunho{border:1px solid var(--linha);border-radius:10px;overflow:hidden;margin-bottom:14px}
.rascunho .cab{display:flex;justify-content:space-between;align-items:center;
 padding:7px 10px 7px 12px;background:rgba(127,127,127,.07);border-bottom:1px solid var(--linha)}
.rascunho .cab button{padding:3px 10px;font-size:12px}
pre.cod{font-family:var(--mono);font-size:12px;line-height:1.5;overflow-x:auto;
 padding:12px 14px;margin:0;background:rgba(127,127,127,.09);border-radius:0}
pre.cod.yaml{background:transparent}
.lacuna details{border-top:1px solid var(--linha);padding-top:12px}
.lacuna summary{display:inline-block;border:1px solid var(--linha);border-radius:8px;
 padding:5px 12px;color:var(--txt);font-size:12.5px;list-style:none}
.lacuna summary::-webkit-details-marker{display:none}
.lacuna summary::before{content:"\\25b8 ";color:var(--sub)}
.lacuna details[open] summary::before{content:"\\25be "}
.lacuna summary:hover{border-color:var(--acao);color:var(--acao)}
.lacuna details pre.cod{border-radius:8px;margin:4px 0 12px}
.cab-sql{margin-top:8px}
@media (max-width:720px){
 .triagem .item{grid-template-columns:22px 1fr;row-gap:6px}
 .triagem .item .medidor,.triagem .item .tag,.triagem .item .sql-n{grid-column:2}
 .triagem .item .sql-n{text-align:left}
}
"""

# Estilos só da home (/). Mesma razão do CSS_DOSSIE: regras que nenhuma outra
# página usa, todas as cores saindo das variáveis do tema.
CSS_HOME = """
.topo-app{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;
 flex-wrap:wrap;margin-bottom:20px}
.topo-app h1{margin:0}
.topo-app .marca{color:var(--acao);font-family:var(--mono);font-size:19px}
.menu{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.menu a{text-decoration:none;border:1px solid var(--linha);border-radius:8px;
 padding:6px 12px;color:var(--txt);font-size:13px;white-space:nowrap}
.menu a:hover{border-color:var(--acao);color:var(--acao)}
.menu .quem{color:var(--sub);font-size:13px;margin-left:4px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;
 margin-bottom:18px}
.kpi{background:var(--card);border:1px solid var(--linha);border-radius:12px;
 padding:14px 16px;display:flex;flex-direction:column;gap:2px}
.kpi b{font-size:26px;line-height:1.15;font-variant-numeric:tabular-nums}
.kpi span{color:var(--sub);font-size:12px}
.kpi.alerta b{color:var(--erro)}
.kpi .rot{font-size:11px}
.lista{padding:0;overflow:hidden}
.lista-topo{display:flex;gap:12px;align-items:center;flex-wrap:wrap;
 padding:14px 18px;border-bottom:1px solid var(--linha)}
.lista-topo h2{margin:0;flex:none}
.lista-topo input[type=search]{font:inherit;font-size:13px;padding:7px 12px;
 border-radius:8px;border:1px solid var(--linha);background:var(--bg);
 color:var(--txt);min-width:220px;flex:1;max-width:340px}
.lista-topo input[type=search]:focus{outline:none;border-color:var(--acao)}
.lista-topo .vagas{margin-left:auto}
.cab-colunas,.pessoa{display:grid;grid-template-columns:44px minmax(170px,1fr) 2.1fr 150px auto;
 gap:14px;align-items:center;padding:14px 18px}
.cab-colunas{padding:9px 18px;font-size:11px;text-transform:uppercase;
 letter-spacing:.05em;color:var(--sub);border-bottom:1px solid var(--linha)}
.pessoa{border-bottom:1px solid var(--linha)}
.pessoa:last-child{border-bottom:0}
.pessoa:hover{background:rgba(127,127,127,.05)}
.avatar{width:40px;height:40px;border-radius:12px;display:flex;align-items:center;
 justify-content:center;font-weight:700;font-size:14px;color:#fff;flex:none;
 background:hsl(var(--h) 42% 46%);letter-spacing:.02em}
.quem{display:flex;flex-direction:column;gap:1px;min-width:0}
.quem strong{font-size:14.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.quem .mono{font-size:12px}
.uso{display:flex;flex-direction:column;gap:3px;font-size:13.5px}
.uso .qdo{display:flex;align-items:center;gap:7px;font-weight:600;
 font-variant-numeric:tabular-nums}
.ponto{width:8px;height:8px;border-radius:50%;flex:none;background:var(--sub);opacity:.45}
.ponto.on{background:var(--ok);opacity:1;box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 22%,transparent)}
.ponto.warm{background:var(--aviso);opacity:1}
.vol{height:4px;border-radius:999px;background:rgba(127,127,127,.16);max-width:110px}
.vol i{display:block;height:100%;border-radius:999px;background:var(--acao);opacity:.75}
.pessoa .acoes{display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end}
.pessoa .acoes button,.pessoa .acoes a{font-size:12px;padding:5px 10px;border-radius:7px;
 border:1px solid var(--linha);background:transparent;color:var(--sub);
 text-decoration:none;cursor:pointer;white-space:nowrap}
.pessoa .acoes button:hover,.pessoa .acoes a:hover{border-color:var(--acao);color:var(--acao)}
.pessoa .acoes button.perigo:hover{border-color:var(--erro);color:var(--erro)}
.pessoa .bancos label{background:transparent}
.btn-novo{text-decoration:none;background:var(--acao);color:#fff;border-radius:8px;
 padding:7px 14px;font-size:13px;white-space:nowrap}
.btn-novo:hover{opacity:.9}
.usuario{display:flex;align-items:center;gap:10px;border:1px solid var(--linha);
 border-radius:999px;padding:5px 6px;background:var(--card);margin-left:6px}
.avatar.mini{width:30px;height:30px;border-radius:50%;font-size:11.5px}
.u-info{display:flex;flex-direction:column;line-height:1.25;padding-right:4px}
.u-info strong{font-size:13px}
.u-info span{color:var(--sub);font-size:11px;font-family:var(--mono)}
.usuario .sair{border:1px solid transparent;background:rgba(127,127,127,.10);
 color:var(--sub);border-radius:999px;padding:6px 13px;font-size:12px;cursor:pointer}
.usuario .sair:hover{color:var(--erro);border-color:var(--erro);background:transparent}
.modal{border:1px solid var(--linha);border-radius:14px;padding:0;background:var(--card);
 color:var(--txt);width:min(560px,calc(100vw - 32px));box-shadow:0 24px 60px rgba(0,0,0,.35)}
.modal::backdrop{background:rgba(10,12,16,.55);backdrop-filter:blur(2px)}
.modal-corpo{padding:20px 22px}
.modal-topo{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.modal-topo h2{margin:0}
.modal .fechar{border:0;background:transparent;color:var(--sub);font-size:22px;
 line-height:1;padding:2px 8px;border-radius:8px;cursor:pointer}
.modal .fechar:hover{color:var(--txt);background:rgba(127,127,127,.12)}
.modal-form{display:flex;flex-direction:column;gap:10px}
.modal-form input[type=text]{min-width:0;width:100%}
.modal-rotulo{font-size:12px;text-transform:uppercase;letter-spacing:.05em;
 color:var(--sub);margin-top:4px}
.modal-acoes{display:flex;justify-content:flex-end;gap:8px;margin-top:12px;
 padding-top:14px;border-top:1px solid var(--linha)}
@media (max-width:900px){
 .cab-colunas{display:none}
 .pessoa{grid-template-columns:44px 1fr;row-gap:10px}
 .pessoa .bancos-col,.pessoa .uso,.pessoa .acoes{grid-column:2}
 .pessoa .acoes{justify-content:flex-start}
}
"""

JS = """
function copiar(txt,b){navigator.clipboard.writeText(txt).then(()=>{
 const t=b.textContent;b.textContent='copiado!';setTimeout(()=>b.textContent=t,1200);});}
function filtrar(q){q=q.trim().toLowerCase();let n=0;
 document.querySelectorAll('.pessoa').forEach(e=>{
  const v=!q||e.dataset.busca.indexOf(q)>=0;e.style.display=v?'':'none';if(v)n++;});
 const r=document.getElementById('nres');if(r)r.textContent=n;}
"""


def _esc(v) -> str:
    return html.escape(str(v), quote=True)


def _caixas_novo() -> str:
    """Caixas do formulário de criação: já vêm com o padrão de menor privilégio."""
    fora = []
    for chave, rotulo, descricao, sensivel in T.PERFIS:
        marcado = " checked" if chave in T.PERFIS_INICIAIS else ""
        classe = " class=\"sens\"" if sensivel else ""
        fora.append(
            f'<label{classe} title="{_esc(descricao)}">'
            f'<input type="checkbox" name="perfis" value="{chave}"{marcado}>'
            f"<span>{_esc(rotulo)}</span></label>"
        )
    return "".join(fora)


def _caixas_bancos(p: T.Pessoa) -> str:
    """Uma caixa por banco + salvar, num form só (server-rendered, sem JS)."""
    caixas = []
    for chave, rotulo, descricao, sensivel in T.PERFIS:
        marcado = " checked" if chave in p.perfis else ""
        classe = " class=\"sens\"" if sensivel else ""
        caixas.append(
            f'<label{classe} title="{_esc(descricao)}">'
            f'<input type="checkbox" name="perfis" value="{chave}"{marcado}>'
            f"<span>{_esc(rotulo)}</span></label>"
        )
    return (
        f'<form method="post" action="/perfis" class="bancos">{_hidden(p.label)}'
        + "".join(caixas)
        + "<button>salvar</button></form>"
    )


def _iniciais(nome: str) -> str:
    partes = [x for x in nome.split() if x]
    if not partes:
        return "?"
    if len(partes) == 1:
        return partes[0][:2].upper()
    return (partes[0][0] + partes[-1][0]).upper()


def _matiz(label: str) -> int:
    """Cor estável do avatar. hashlib e não hash(): o hash() do Python é salteado
    por processo, e a cor de cada pessoa mudaria a cada restart do painel."""
    return int(hashlib.md5(label.encode()).hexdigest()[:6], 16) % 360


def _frescor(ultimo_uso: str) -> str:
    """'on' usou hoje, 'warm' usou na janela, 'off' nunca — lê o texto do
    _humano() porque é o único formato que chega até aqui."""
    if not ultimo_uso:
        return "off"
    if ultimo_uso == "agora" or ultimo_uso.endswith("min") or ultimo_uso.endswith(" h"):
        return "on"
    return "warm"


def _prazo(p: T.Pessoa) -> str:
    """Chip do vencimento: é a informação que decide a ação da tela (renovar).

    Três estados, porque as ações são diferentes: sem prazo (bloco legado — o
    acesso não vence, e isso é pendência), vencendo (avisar a pessoa antes de
    cortar) e vencido (só renovar volta a funcionar; o token dela continua o
    mesmo).
    """
    d = p.dias_restantes
    if d is None:
        return ('<span class="prazo sem" title="bloco sem a linha X-Db-Expira: '
                'este acesso não vence. Renove para passar a valer a política.">'
                'sem prazo</span>')
    quando = _esc(f"{datetime.strptime(p.expira[:10], '%Y-%m-%d'):%d/%m/%Y}")
    if d < 0:
        return (f'<span class="prazo venceu" title="não consulta mais nenhum banco">'
                f'venceu {quando}</span>')
    if d <= T.AVISO_DIAS:
        dias = "vence hoje" if d == 0 else f"{d}d"
        return f'<span class="prazo perto" title="vence em {quando}">{dias}</span>'
    return f'<span class="prazo ok" title="vence em {quando}">{d}d</span>'


def _desde(criado: str) -> str:
    try:
        return f"desde {datetime.strptime(criado[:16], '%Y-%m-%d %H:%M'):%d/%m/%Y}"
    except ValueError:
        return criado or ""


def _cartao_pessoa(p: T.Pessoa, uso_disponivel: bool, max_chamadas: int) -> str:
    frescor = _frescor(p.ultimo_uso)
    if not uso_disponivel:
        uso = '<span class="nada">&mdash;</span>'
    elif p.ultimo_uso:
        pct = max(4, round(100 * p.chamadas / max_chamadas)) if max_chamadas else 0
        uso = (f'<span class="qdo"><span class="ponto {frescor}"></span>'
               f'{_esc(p.ultimo_uso)}</span>'
               f'<span class="rot">{p.chamadas} chamadas</span>'
               f'<div class="vol" title="{p.chamadas} de {max_chamadas} do mais ativo">'
               f'<i style="width:{pct}%"></i></div>')
    else:
        uso = (f'<span class="qdo"><span class="ponto off"></span>'
               f'<span class="nada">sem uso em {DIAS}d</span></span>')

    busca = _esc(f"{p.exibicao} @{p.label}".lower())
    return f"""
    <div class="pessoa" data-busca="{busca}">
      <div class="avatar" style="--h:{_matiz(p.label)}">{_esc(_iniciais(p.exibicao))}</div>
      <div class="quem">
        <strong>{_esc(p.exibicao)}</strong>
        <span class="rot mono">@{_esc(p.label)}</span>
        <span class="rot">{_esc(_desde(p.criado)) or '&mdash;'}</span>
        {_prazo(p)}
      </div>
      <div class="bancos-col">{_caixas_bancos(p)}</div>
      <div class="uso">{uso}</div>
      <div class="acoes">
        <a href="/consultas?pessoa={_esc(p.label)}">consultas</a>
        <form method="post" action="/renovar" onsubmit="return confirm('Renovar o acesso de {_esc(p.exibicao)} por mais {T.PRAZO_DIAS} dias? O token dela NÃO muda.')">{_hidden(p.label)}<button>renovar</button></form>
        <form method="post" action="/testar">{_hidden(p.label)}<button>testar</button></form>
        <form method="post" action="/rotacionar" onsubmit="return confirm('Gerar um token NOVO para {_esc(p.exibicao)}? O atual para de funcionar na hora.')">{_hidden(p.label)}<button>rotacionar</button></form>
        <form method="post" action="/revogar">{_hidden(p.label)}<button class="perigo">revogar&hellip;</button></form>
      </div>
    </div>"""


def _hidden(label: str) -> str:
    return (
        f'<input type="hidden" name="label" value="{_esc(label)}">'
        f'<input type="hidden" name="csrf" value="{_CSRF}">'
    )


def _caixa_resultado(r: dict | None) -> str:
    if not r:
        return ""
    if r.get("erro"):
        return f'<div class="card erro"><strong>Não deu.</strong> {_esc(r["erro"])}</div>'
    if r.get("token"):
        token = r["token"]
        cmd_cc = (
            f"claude mcp add --transport http {NOME_SERVIDOR} {ENDPOINT} "
            f'--scope user --header "Authorization: Bearer {token}"'
        )
        return f"""
        <div class="card ok">
          <h2>{_esc(r['titulo'])}</h2>
          <p class="sub">Já valendo no proxy — o reload foi feito. Entregue por canal privado
          e cole <strong>só os 64 caracteres</strong>: token com espaço ou quebra de linha faz
          o cliente recusar o header antes de sequer conectar.</p>
          <div class="tok"><code class="mono">{_esc(token)}</code>
            <button type="button" onclick="copiar('{_esc(token)}',this)">copiar token</button></div>
          <p class="sub" style="margin-top:14px">Claude Code (qualquer SO), na máquina da pessoa:</p>
          <pre>{_esc(cmd_cc)}</pre>
          <p class="sub">Claude Desktop: veja <a href="/comandos" target="_blank" rel="noopener">Como conectar</a>.</p>
        </div>"""
    return f'<div class="card ok">{_esc(r.get("mensagem", "Feito."))}</div>'


def _pagina(request: Request) -> HTMLResponse:
    resultado = _RESULTADOS.pop(request.query_params.get("r", ""), None)
    try:
        pessoas, u = T.com_uso(DIAS)
        falha = ""
    except T.ErroDeToken as e:
        pessoas, u, falha = [], T.Uso(), str(e)

    # Quem usou por último vem primeiro: numa tela de operação, o topo é onde o
    # olho procura quem está ativo AGORA. O timestamp cru vem do agregado do log
    # (o texto humanizado da Pessoa não ordena). Empate — inclusive todo mundo
    # sem uso — cai para ordem alfabética.
    ult = {lbl: d.get("ultimo", 0.0) for lbl, d in u.por_pessoa.items()}
    pessoas.sort(key=lambda p: (-ult.get(p.label, 0.0), p.exibicao.lower()))

    max_chamadas = max((p.chamadas for p in pessoas), default=0)

    if falha:
        corpo = f'<div class="pessoa" style="grid-template-columns:1fr"><span class="erro">{_esc(falha)}</span></div>'
    elif pessoas:
        corpo = "".join(_cartao_pessoa(p, u.disponivel, max_chamadas) for p in pessoas)
    else:
        corpo = ('<div class="pessoa" style="grid-template-columns:1fr">'
                 '<span class="nada">Nenhum acesso configurado ainda — crie o primeiro abaixo.</span></div>')

    aviso = ""
    if not u.disponivel and not falha:
        aviso = (
            '<div class="card aviso">O log de acesso do Caddy ainda não existe, então a coluna '
            '<em>uso</em> fica vazia. Ele aparece na primeira chamada depois que o proxy '
            'subir com o log em arquivo.</div>'
        )

    # ------------------------------------------------------------- indicadores
    ativos_hoje = sum(1 for p in pessoas if _frescor(p.ultimo_uso) == "on")
    vencidos = sum(1 for p in pessoas if p.vencido)
    sem_prazo = sum(1 for p in pessoas if p.dias_restantes is None)
    vencendo = sum(1 for p in pessoas
                   if p.dias_restantes is not None and 0 <= p.dias_restantes <= T.AVISO_DIAS)
    # Um KPI só para os três estados, porque a ação é a mesma (renovar) e três
    # quadros de prazo empurrariam os de uso para fora da primeira dobra.
    if vencidos or sem_prazo:
        n_prazo, rot_prazo = (vencidos or sem_prazo), (
            f"{'vencido' if vencidos == 1 else 'vencidos'}"
            if vencidos else "sem prazo (renove para aplicar)")
        classe_prazo = " alerta"
    elif vencendo:
        n_prazo, rot_prazo, classe_prazo = vencendo, f"vencendo em {T.AVISO_DIAS}d", ""
    else:
        n_prazo, rot_prazo, classe_prazo = 0, "vencidos", ""
    kpi_prazo = (f'<div class="kpi{classe_prazo}"><b>{n_prazo}</b>'
                 f'<span>{rot_prazo}</span></div>')
    total_chamadas = sum(p.chamadas for p in pessoas)
    if u.disponivel:
        # Milhar com ponto (pt-BR) formatado ANTES do template: um .replace no
        # bloco inteiro comeria também as vírgulas do texto corrido.
        chamadas_fmt = f"{total_chamadas:,}".replace(",", ".")
        kpis = f"""
  <div class="kpis">
    <div class="kpi"><b>{len(pessoas)}</b><span>{'pessoa' if len(pessoas) == 1 else 'pessoas'} com acesso</span></div>
    <div class="kpi"><b>{ativos_hoje}</b><span>ativas hoje</span></div>
    <div class="kpi"><b>{chamadas_fmt}</b><span>chamadas em {DIAS}d</span></div>
    <div class="kpi{' alerta' if u.recusadas else ''}"><b>{u.recusadas}</b>
      <span>recusadas (401) em {DIAS}d</span>
      {'<span class="rot">token errado, revogado ou tentativa de fora</span>' if u.recusadas else ''}</div>
    {kpi_prazo}
  </div>"""
    else:
        kpis = f"""
  <div class="kpis">
    <div class="kpi"><b>{len(pessoas)}</b><span>{'pessoa' if len(pessoas) == 1 else 'pessoas'} com acesso</span></div>
    {kpi_prazo}
  </div>"""

    sair = ""
    if SENHA:
        sair = (f'<div class="usuario"><form method="post" action="/sair">'
                f'<input type="hidden" name="csrf" value="{_CSRF}">'
                f'<button class="sair" title="encerrar a sessão">sair</button></form></div>')

    return HTMLResponse(f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acessos do MCP {_esc(NOME_SERVIDOR)}</title><style>{CSS}{CSS_HOME}</style></head>
<body><div class="wrap largo">
  <div class="topo-app">
    <div>
      <h1>Acessos do MCP <span class="marca">{_esc(NOME_SERVIDOR)}</span></h1>
      <p class="sub" style="margin:4px 0 0">Endpoint <code class="mono">{_esc(ENDPOINT)}</code></p>
    </div>
    <nav class="menu">
      <a href="/dossie">Relatório de lacunas</a>
      <a href="/perguntas">Catálogo de perguntas</a>
      <a href="/consultas">Trilha de consultas</a>
      <a href="/comandos" target="_blank" rel="noopener">Como conectar &#8599;</a>
      {sair}
    </nav>
  </div>

  {aviso}
  {_caixa_resultado(resultado)}
  {kpis}

  <div class="card lista">
    <div class="lista-topo">
      <h2>Pessoas</h2>
      <input type="search" placeholder="filtrar por nome ou @login&hellip;"
             oninput="filtrar(this.value)" aria-label="filtrar pessoas">
      <span class="rot"><span id="nres">{len(pessoas)}</span> exibida(s)</span>
      <button type="button" class="btn-novo vagas"
              onclick="document.getElementById('novo').showModal()">+ novo acesso</button>
    </div>
    <div class="cab-colunas">
      <span></span><span>Pessoa</span><span>Bancos liberados</span>
      <span>Uso ({DIAS}d)</span><span style="text-align:right">Ações</span>
    </div>
    {corpo}
  </div>

  <!-- <dialog> nativo: showModal() dá backdrop, foco preso e Esc de graça.
       O clique no backdrop fecha porque só o próprio dialog recebe o evento
       fora do .modal-corpo (event.target === this). O form continua o mesmo
       POST /criar com CSRF — modal é só apresentação. -->
  <dialog id="novo" class="modal" onclick="if(event.target===this)this.close()">
    <div class="modal-corpo">
      <div class="modal-topo">
        <h2>Novo acesso</h2>
        <button type="button" class="fechar" aria-label="fechar"
                onclick="this.closest('dialog').close()">&times;</button>
      </div>
      <form method="post" action="/criar" class="modal-form">
        <input type="hidden" name="csrf" value="{_CSRF}">
        <label class="modal-rotulo" for="novo-nome">Nome da pessoa</label>
        <input type="text" id="novo-nome" name="nome" autofocus
               placeholder="ex.: Maria Silva" required>
        <label class="modal-rotulo">Bancos liberados</label>
        <span class="bancos">{_caixas_novo()}</span>
        <p class="sub" style="margin:2px 0 0">O rótulo <code class="mono">@nome_sobrenome</code> é derivado do
        nome — é o bloco que se apaga para revogar. Cada pessoa recebe <strong>ferramenta, não banco</strong>:
        as credenciais dos bancos ficam só no servidor. As caixas tracejadas são dado de pessoa
        e novo acesso nasce sem elas.</p>
        <div class="modal-acoes">
          <button type="button" onclick="this.closest('dialog').close()">cancelar</button>
          <button class="primario">Gerar token</button>
        </div>
      </form>
    </div>
  </dialog>

  <footer>
    <p><strong>Bancos liberados</strong> vale por chamada, não por sessão: desmarcar um
    banco tira o acesso a ele na consulta seguinte da pessoa, sem pedir para ela
    reconectar. Quem tenta um banco que não tem recebe uma recusa explícita do MCP
    (nada de erro de conexão). As caixas tracejadas são os bancos marcados como
    sensíveis (<code>PERFIS_SENSIVEIS</code>): dado de pessoa.</p>
    <p>As tools de conhecimento (<code>listar_indice</code>,
    <code>recuperar_contexto</code>) não são filtradas: elas mostram schema e notas,
    não dado. A trava está em <code>consultar_sql</code> e <code>validar_sql</code>.</p>
  </footer>
</div><script>{JS}</script></body></html>""")


def _campo(valor: str, rotulo: str = "") -> str:
    """Campo somente-leitura com botão de copiar (o valor inteiro, sem cortar)."""
    tit = f'<p class="sub" style="margin:14px 0 0">{_esc(rotulo)}</p>' if rotulo else ""
    return (
        f'{tit}<div class="campo">'
        f'<input type="text" value="{_esc(valor)}" readonly onclick="this.select()">'
        f"<button type=\"button\" onclick=\"copiar(this.previousElementSibling.value,this)\">copiar</button>"
        "</div>"
    )


def _pagina_comandos() -> HTMLResponse:
    """Endpoint e o jeito de configurar cada cliente, fora da tela de acessos."""
    cmd_cc = (
        f"claude mcp add --transport http {NOME_SERVIDOR} {ENDPOINT} "
        '--scope user --header "Authorization: Bearer COLE_O_TOKEN"'
    )
    # O app de desktop só aceita servidor stdio no config: a ponte mcp-remote faz
    # o meio de campo com o HTTP. O header vai SEM espaço depois dos dois-pontos e
    # com o valor numa variável de ambiente — é assim que o mcp-remote separa nome
    # e valor sem o shell do sistema quebrar o argumento.
    extra = ', "--allow-http"' if ENDPOINT.startswith("http://") else ""
    cfg_desktop = (
        '{\n'
        '  "mcpServers": {\n'
        f'    "{NOME_SERVIDOR}": {{\n'
        '      "command": "npx",\n'
        f'      "args": ["-y", "mcp-remote", "{ENDPOINT}"{extra},\n'
        '               "--header", "Authorization:${AUTH_HEADER}"],\n'
        '      "env": {"AUTH_HEADER": "Bearer COLE_O_TOKEN"}\n'
        '    }\n'
        '  }\n'
        '}'
    )
    if SENHA:
        portas = "pede a senha do painel para entrar"
    else:
        portas = ("<strong>não pede login</strong> — por isso só sobe publicado em "
                  "loopback. Para abrir na rede interna, defina <code>ADMIN_SENHA</code> "
                  "no <code>.env.admin</code>; sem ela o painel se recusa a subir exposto")
    exposicao = f"Este painel responde na porta <code>{PORTA}</code> do container e {portas}."
    aviso_http = ""
    if ENDPOINT.startswith("http://"):
        aviso_http = ('<p class="sub" style="margin:10px 0 0">O endpoint é HTTP puro: o '
                      'token trafega sem criptografia. Use só em rede interna confiável, '
                      'ou ponha TLS na frente do proxy.</p>')
    return HTMLResponse(f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Como conectar ao MCP {_esc(NOME_SERVIDOR)}</title><style>{CSS}</style></head>
<body><div class="wrap" style="max-width:820px">
  <a class="link-topo" href="/">&#8592; Acessos</a>
  <h1>Como conectar</h1>
  <p class="sub">Troque <code>COLE_O_TOKEN</code> pelo token da pessoa. Ele aparece
     <strong>uma vez</strong>, na hora de gerar ou rotacionar — se foi perdido, rotacione
     e entregue o novo.</p>

  <div class="card">
    <h2>Servidor</h2>
    {_campo(ENDPOINT, "Endpoint do MCP (é este link que o cliente configura):")}
    {aviso_http}
  </div>

  <div class="card">
    <h2>Claude Code &middot; qualquer SO</h2>
    <p class="sub" style="margin:0 0 4px">Fala HTTP direto — sem Node e sem ponte. Se já
       existir uma entrada antiga: <code>claude mcp remove {_esc(NOME_SERVIDOR)}</code>.
       Sessão do <code>claude</code> já aberta não relê a configuração: abra uma nova.</p>
    {_campo(cmd_cc)}
  </div>

  <div class="card">
    <h2>Claude Desktop</h2>
    <p class="sub" style="margin:0 0 6px">O app só aceita servidor <em>stdio</em> no
       <code>claude_desktop_config.json</code>, então a ponte <code>mcp-remote</code> (Node)
       faz a ligação com o HTTP. Acrescente o bloco abaixo ao arquivo (Configurações &rarr;
       Desenvolvedor &rarr; Editar configuração) e reinicie o app por completo.</p>
    <pre>{_esc(cfg_desktop)}</pre>
    <p class="sub" style="margin:10px 0 0">A ponte deixa <strong>mais uma cópia do token</strong>
       em texto no perfil da pessoa. Quem trabalha no Claude Code não precisa dela.</p>
  </div>

  <footer>{exposicao}</footer>
</div><script>{JS}</script></body></html>""")


def _quando(ts: str) -> str:
    """ISO -> 'hoje 14:02' / '18/08 09:31' (a trilha é lida por gente)."""
    try:
        q = datetime.fromisoformat(ts)
    except Exception:
        return ts
    hoje = datetime.now().date()
    if q.date() == hoje:
        return f"hoje {q:%H:%M}"
    return f"{q:%d/%m %H:%M}"


def _pagina_consultas(pessoa_filtro: str | None) -> HTMLResponse:
    """Quem consultou o quê. Lê os .jsonl que o mcp_server escreve."""
    registros = auditoria.ler(dias=DIAS, limite=250, pessoa=pessoa_filtro)
    resumo = auditoria.resumo(dias=DIAS)

    pessoas = sorted(resumo)
    filtros = ['<a href="/consultas" class="' + ("on" if not pessoa_filtro else "") + '">todas</a>']
    for nome in pessoas:
        d = resumo[nome]
        marca = " on" if pessoa_filtro == nome else ""
        filtros.append(
            f'<a class="{marca.strip()}" href="/consultas?pessoa={_esc(nome)}">'
            f"{_esc(nome)} <span class=\"rot\">({d['consultas']})</span></a>"
        )

    if not registros:
        corpo = ('<tr><td colspan="5" class="nada">Nada registrado nos últimos '
                 f'{DIAS} dias. A trilha começa na primeira consulta depois que o '
                 'mcp subiu com AUDIT_DIR.</td></tr>')
    else:
        linhas = []
        for r in registros:
            neg = not r.get("ok")
            motivo = r.get("motivo") or ""
            resultado = (f'<span class="neg">recusado</span><div class="rot">{_esc(motivo)}</div>'
                         if neg else
                         f"{r.get('linhas', '?')} linhas"
                         + (' <span class="rot">(truncado)</span>' if r.get("truncado") else ""))
            tabelas = ", ".join(r.get("tabelas") or [])
            linhas.append(f"""
            <tr>
              <td class="rot" style="white-space:nowrap">{_esc(_quando(r['ts']))}</td>
              <td><strong>{_esc(r.get('pessoa', '?'))}</strong>
                  <div class="rot">{_esc(r.get('perfil', ''))} · {_esc(r.get('tool', ''))}</div></td>
              <td><div class="sql">{_esc(r.get('sql', '') or '&mdash;')}</div>
                  {f'<div class="rot">tabelas: {_esc(tabelas)}</div>' if tabelas else ''}</td>
              <td style="white-space:nowrap">{resultado}</td>
              <td class="rot" style="white-space:nowrap">{r.get('ms', '')}{' ms' if r.get('ms') else ''}</td>
            </tr>""")
        corpo = "".join(linhas)

    total = sum(d["consultas"] for d in resumo.values())
    negadas = sum(d["negadas"] for d in resumo.values())
    return HTMLResponse(f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trilha de consultas — {_esc(ENDPOINT)}</title><style>{CSS}</style></head>
<body><div class="wrap largo">
  <a class="link-topo" href="/">&#8592; Acessos</a>
  <h1>Trilha de consultas</h1>
  <p class="sub"><strong>{total}</strong> consultas em {DIAS} dias
     {f'· <strong class="neg">{negadas}</strong> recusadas' if negadas else ''}
     · o SQL de cada uma, com quem rodou e quanto voltou</p>

  <div class="filtros">{"".join(filtros)}</div>

  <div class="card">
    <table>
      <thead><tr><th>Quando</th><th>Quem</th><th>Consulta</th><th>Resultado</th><th>Tempo</th></tr></thead>
      <tbody>{corpo}</tbody>
    </table>
  </div>

  <footer>Gravada pelo servidor MCP em <code>auditoria/consultas-AAAA-MM-DD.jsonl</code>,
  um arquivo por dia. Guarda o texto da consulta de propósito — sem ele, "alguém rodou
  algo no banco de RH" não responde nada. Por isso o arquivo fica fora do git e merece o
  mesmo cuidado do <code>.env</code>: expurgo é <code>rm</code> dos dias antigos.</footer>
</div></body></html>""")


def _pagina_perguntas(dias: int, pessoa_filtro: str | None) -> HTMLResponse:
    """O que as pessoas perguntam — e o que a base ainda não responde.

    Esta tela não é auditoria (isso é `/consultas`). Ela existe para virar
    trabalho: a primeira tabela é a fila de notas a escrever, e a segunda mostra,
    por pergunta, o que a base entregou e qual query funcionou — que é o rascunho
    do exemplo SQL da nota nova.
    """
    grupos = perguntas.catalogo(dias=dias, pessoa=pessoa_filtro)
    fila = perguntas.lacunas(grupos=grupos)
    r = perguntas.resumo(grupos)

    def _janelas():
        saida = []
        for d in (7, 30, 90):
            alvo = f"/perguntas?dias={d}" + (f"&pessoa={_esc(pessoa_filtro)}" if pessoa_filtro else "")
            saida.append(f'<a class="{"on" if d == dias else ""}" href="{alvo}">{d} dias</a>')
        return "".join(saida)

    pessoas = sorted({p for g in grupos for p in g["pessoas"]})
    filtros = [f'<a href="/perguntas?dias={dias}" class="{"" if pessoa_filtro else "on"}">todas</a>']
    for nome in pessoas:
        marca = "on" if pessoa_filtro == nome else ""
        filtros.append(f'<a class="{marca}" href="/perguntas?dias={dias}&pessoa={_esc(nome)}">'
                       f"{_esc(nome)}</a>")

    # ---------------------------------------------------------------- lacunas
    if not fila:
        # "sem lacuna" e "sem pergunta" são estados diferentes: dizer que a base
        # respondeu tudo quando ninguém perguntou nada seria elogio falso.
        motivo = ("toda pergunta recuperou nota forte o bastante" if grupos
                  else "nenhuma pergunta registrada ainda")
        corpo_fila = ('<tr><td colspan="4" class="nada">Nenhuma lacuna nos últimos '
                      f'{dias} dias — {motivo}.</td></tr>')
    else:
        linhas = []
        for g in fila[:60]:
            # Três motivos, três etiquetas. Antes eram dois e o `else` pegava
            # tudo: com `contexto_fraco` no meio, a tela chamaria de "schema sem
            # nota" o caso em que a nota veio — só que a errada.
            etiqueta = {
                "nada_recuperado": '<span class="tag alerta">não recuperou nada</span>',
                "contexto_fraco": '<span class="tag alerta">nota fraca</span>',
            }.get(g["motivo"], '<span class="tag">schema sem nota</span>')
            if g["motivo"] == "contexto_fraco" and g["forca"] is not None:
                etiqueta += f'<div class="rot">força {g["forca"]:.2f} de {perguntas.FORCA_MINIMA:.2f}</div>'
            pistas = ", ".join(t for t, _ in g["tabelas"][:5])
            linhas.append(f"""
            <tr>
              <td class="perg">{_esc(g['pergunta'])}
                  {f'<div class="rot">tabelas recuperadas: {_esc(pistas)}</div>' if pistas else ''}</td>
              <td>{etiqueta}</td>
              <td style="white-space:nowrap"><strong>{g['vezes']}</strong>
                  <span class="rot">{'vez' if g['vezes'] == 1 else 'vezes'}</span></td>
              <td class="rot" style="white-space:nowrap">{_esc(", ".join(g['pessoas'][:3]))}
                  <div>{_esc(_quando(g['ultimo']))}</div></td>
            </tr>""")
        corpo_fila = "".join(linhas)

    # --------------------------------------------------------------- catálogo
    if not grupos:
        corpo_cat = ('<tr><td colspan="4" class="nada">Nada catalogado nos últimos '
                     f'{dias} dias. O catálogo começa na primeira pergunta depois que o '
                     'mcp subiu com PERGUNTAS_DIR.</td></tr>')
    else:
        linhas = []
        for g in grupos[:150]:
            notas = "".join(f'<span class="tag">{_esc(n)}</span> ' for n, _ in g["notas"][:4])
            tabelas = ", ".join(t for t, _ in g["tabelas"][:6])
            entregou = (notas or '<span class="rot">nenhuma nota</span>')
            if tabelas:
                entregou += f'<div class="rot">{_esc(tabelas)}</div>'
            if g["sqls"]:
                blocos = "".join(
                    f'<div class="rot" style="margin-top:6px">{_esc(s.get("perfil") or "?")} · '
                    f'{s.get("linhas", "?")} linhas · {_esc(_quando(s["ts"]))}</div>'
                    f'<div class="sql" style="max-height:none">{_esc(s.get("sql") or "")}</div>'
                    for s in g["sqls"])
                plural = ("query que respondeu" if g["respostas"] == 1
                          else "queries que responderam")
                queries = (f"<details><summary>{g['respostas']} {plural}"
                           f"</summary>{blocos}</details>")
            else:
                queries = '<span class="rot">&mdash;</span>'
            variantes = (f'<div class="rot">{g["variantes"]} redações diferentes</div>'
                         if g["variantes"] > 1 else "")
            linhas.append(f"""
            <tr>
              <td class="perg">{_esc(g['pergunta'])}{variantes}
                  <div class="rot">{_esc(", ".join(g['pessoas'][:4]))} · {_esc(_quando(g['ultimo']))}</div></td>
              <td style="white-space:nowrap"><strong>{g['vezes']}</strong>
                  {f'<div class="rot neg">{g["sem_contexto"]} sem contexto</div>' if g['sem_contexto'] else ''}</td>
              <td>{entregou}</td>
              <td>{queries}</td>
            </tr>""")
        corpo_cat = "".join(linhas)

    return HTMLResponse(f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Catálogo de perguntas — {_esc(ENDPOINT)}</title><style>{CSS}</style></head>
<body><div class="wrap largo">
  <a class="link-topo" href="/consultas" style="margin-right:8px">Trilha de consultas</a>
  <a class="link-topo" href="/">&#8592; Acessos</a>
  <h1>Catálogo de perguntas</h1>
  <p class="sub"><strong>{r['registros']}</strong> perguntas em {dias} dias ·
     <strong>{r['distintas']}</strong> distintas ·
     <strong>{r['respondidas']}</strong> com query que funcionou ·
     {len(fila)} na fila de notas a escrever</p>

  <div class="filtros">{_janelas()}</div>
  <div class="filtros">{"".join(filtros)}</div>

  <div class="card">
    <h2>Fila: notas a escrever</h2>
    <p class="sub" style="margin-bottom:14px">Pergunta que a base não soube responder.
       <strong>não recuperou nada</strong> = assunto ausente, ou vocabulário da pergunta
       fora das notas (candidato a sinônimo no <code>contexto/glossario.txt</code>).
       <strong>schema sem nota</strong> = veio tabela mas nenhuma regra: é onde o modelo
       acerta a coluna e erra o critério.</p>
    <table>
      <thead><tr><th>Pergunta</th><th>Motivo</th><th>Quantas</th><th>Quem · quando</th></tr></thead>
      <tbody>{corpo_fila}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>Mais perguntadas</h2>
    <p class="sub" style="margin-bottom:14px">Agrupadas por semelhança (reformulação da
       mesma pergunta cai na mesma linha). A query que respondeu é o rascunho pronto do
       exemplo SQL da nota.</p>
    <table>
      <thead><tr><th>Pergunta</th><th>Vezes</th><th>O que a base entregou</th><th>Resposta</th></tr></thead>
      <tbody>{corpo_cat}</tbody>
    </table>
  </div>

  <footer>Gravado pelo servidor MCP em <code>perguntas/perguntas-AAAA-MM-DD.jsonl</code>,
  um arquivo por dia. <strong>Não é a trilha de auditoria</strong> — aqui não há recusa nem
  erro de banco: só o que as pessoas quiseram saber e o que a base conseguiu entregar.
  Contém a redação da pergunta, que carrega nome de pessoa e de cliente com naturalidade:
  fica fora do git, 0600, e o expurgo é <code>rm</code> dos dias antigos.</footer>
</div></body></html>""")


# ------------------------------------------------------------------- rotas

async def raiz(request: Request):
    if not _autenticado(request):
        return RedirectResponse("/login", status_code=303)
    return _pagina(request)


async def consultas(request: Request):
    if not _autenticado(request):
        return RedirectResponse("/login", status_code=303)
    return _pagina_consultas(request.query_params.get("pessoa") or None)


async def catalogo_perguntas(request: Request):
    if not _autenticado(request):
        return RedirectResponse("/login", status_code=303)
    try:  # janela pela URL, com teto: ler 10 anos de .jsonl a cada F5 não serve a ninguém
        dias = min(365, max(1, int(request.query_params.get("dias", DIAS_PERGUNTAS))))
    except ValueError:
        dias = DIAS_PERGUNTAS
    return _pagina_perguntas(dias, request.query_params.get("pessoa") or None)


async def dossie(request: Request):
    if not _autenticado(request):
        return RedirectResponse("/login", status_code=303)
    # A data vem da aba, mas o arquivo é escolhido por igualdade contra a lista
    # do diretório (em `_pagina_dossie`), nunca concatenada num caminho: query
    # string não monta nome de arquivo neste painel.
    return _pagina_dossie(request.query_params.get("data") or None)


async def comandos(request: Request):
    if not _autenticado(request):
        return RedirectResponse("/login", status_code=303)
    return _pagina_comandos()


# ---------------------------------------------------------------------------
# Relatório semanal de lacunas (/dossie)
# ---------------------------------------------------------------------------
def _dossies():
    """Relatórios já gerados, do mais novo para o mais velho: [(data, base)].

    `base` é o caminho SEM extensão: o timer escreve `.json` (o que a tela
    desenha) e `.md` (o que se copia para um prompt), e a tela precisa dos dois.
    """
    try:
        nomes = {n.rsplit(".", 1)[0] for n in os.listdir(DOSSIE_DIR)
                 if n.startswith("dossie-") and n.endswith((".md", ".json"))}
    except OSError:
        return []
    saida = [(n[7:], os.path.join(DOSSIE_DIR, n)) for n in nomes]
    saida.sort(reverse=True)
    return saida


def _medidor(forca, piso):
    """Barra de gravidade da lacuna.

    A escala vai de 0 a 0,40 e não de 0 a 1: as forças reais se amontoam entre
    0,09 e 0,28 (mediana 0,28), e numa escala cheia todas as barras ficariam
    iguais — o medidor não mostraria nada. O piso aparece como um traço, para a
    barra ser lida contra ele e não contra o máximo teórico.
    """
    teto = 0.40
    if forca is None:
        return ('<div class="medidor"><div class="barra vazio" style="width:100%">'
                '</div></div><span class="rot">não recuperou nada</span>')
    pct = max(3.0, min(100.0, forca / teto * 100))
    marca = piso / teto * 100
    return (f'<div class="medidor" title="força {forca:.3f} · piso {piso:.2f}">'
            f'<div class="barra" style="width:{pct:.1f}%"></div>'
            f'<div class="piso" style="left:{marca:.1f}%"></div></div>')


_MOTIVOS = {
    "nada_recuperado": ("não recuperou nada", "alerta"),
    "contexto_fraco": ("nota fraca", "alerta"),
    "sem_nota": ("schema sem nota", ""),
}


def _bloco_lacuna(i, g, piso):
    """Um cartão por lacuna: gravidade, quem perguntou, rascunho e o material."""
    rotulo, classe = _MOTIVOS.get(g["motivo"], (g["motivo"], ""))
    forca_txt = "—" if g["forca"] is None else f"{g['forca']:.2f}"

    rascunho = (
        "# rascunho — reveja tudo antes de salvar em NOTES/<banco>/<assunto>/\n"
        f"perfil: {g['perfil']}\n"
        f"tabelas: [{', '.join(g['tabelas'])}]\n"
        "pontes: []   # preencha com as arestas que a SQL abaixo revela"
    )

    chips = [f'<span class="chip">{_esc(p)}</span>' for p in g["pessoas"][:4]]
    if len(g["pessoas"]) > 4:
        chips.append(f'<span class="chip">+{len(g["pessoas"]) - 4}</span>')

    notas = ", ".join(f"<code>{_esc(n)}</code>" for n in g["notas_oferecidas"]) \
        or '<span class="rot">nada</span>'

    if g["sqls"]:
        linhas_sql = []
        for q in g["sqls"]:
            linhas_sql.append(
                f'<div class="rot cab-sql">{_esc(_quando(q["ts"]))} · '
                f'<strong>{_esc(q["pessoa"])}</strong> · perfil {_esc(q["perfil"])} · '
                f'{_esc(q.get("linhas", "?"))} linhas · {_esc(q.get("ms", "?"))} ms</div>'
                f'<pre class="cod">{_esc((q.get("sql") or "").strip())}</pre>')
        material = (f'<details><summary>Ver as {len(g["sqls"])} consultas que '
                    f'responderam (de {g["respostas"]} que deram certo)</summary>'
                    + "".join(linhas_sql) + "</details>")
    else:
        material = ('<p class="rot">Nenhuma consulta bem-sucedida ficou amarrada a '
                    'esta pergunta — o rascunho vai sem exemplo de SQL.</p>')

    return f"""
  <div class="card lacuna" id="l{i}">
    <div class="topo-lacuna">
      <span class="num">{i}</span>
      <h2>{_esc(g['pergunta'])}</h2>
    </div>
    <div class="grav">
      <span class="tag {classe}">{_esc(rotulo)}</span>
      {_medidor(g['forca'], piso)}
      <span class="rot">força <strong>{forca_txt}</strong> · piso {piso:.2f}</span>
    </div>
    <div class="meta">
      <div><span class="rot">perguntada</span>
           <strong>{g['vezes']}x</strong>
           <span class="rot">em {g['variantes']} redação(ões)</span></div>
      <div><span class="rot">por</span> {''.join(chips)}</div>
      <div><span class="rot">período</span>
           {_esc(_quando(g['primeiro']))} &rarr; {_esc(_quando(g['ultimo']))}</div>
      <div><span class="rot">material</span>
           <strong>{g['respostas']}</strong> <span class="rot">consultas OK</span></div>
    </div>
    <p class="rot ofereceu">a base ofereceu: {notas}</p>

    <div class="rascunho">
      <div class="cab">
        <span class="rot">Rascunho do frontmatter</span>
        <button onclick="copiar(this.parentNode.nextElementSibling.textContent,this)"
                type="button">copiar</button>
      </div>
      <pre class="cod yaml">{_esc(rascunho)}</pre>
    </div>

    {material}
  </div>"""


def _pagina_dossie(data: str | None) -> HTMLResponse:
    """O relatório da semana: cada lacuna com o rascunho de nota já montado.

    A tela `/perguntas` responde "o que perguntaram e o que a base entregou".
    Esta responde a pergunta seguinte, que é a que gera trabalho: **qual nota
    escrever agora, e com que material**. Não recalcula nada — publica o que o
    timer gerou e diz quando foi.
    """
    disponiveis = _dossies()
    if not disponiveis:
        return _moldura_dossie(
            "", "", '<div class="card"><p class="nada">Nenhum relatório gerado '
            'ainda. Rode <code>./gerar_dossie.sh</code> no servidor — e agende-o '
            '(cron ou systemd timer) para sair toda semana.</p></div>', [], None)

    base = next((b for d, b in disponiveis if d == data), disponiveis[0][1])
    atual = os.path.basename(base)[7:]
    try:
        with open(base + ".json", encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        # Relatório antigo (só .md) ou json quebrado: mostra o markrown cru em
        # vez de uma tela vazia — o conteúdo é o que interessa, não o enfeite.
        try:
            with open(base + ".md", encoding="utf-8") as fh:
                cru = fh.read()
        except OSError:
            cru = f"não consegui ler {base}.json nem {base}.md: {e}"
        return _moldura_dossie(atual, "", f'<div class="card"><pre>{_esc(cru)}</pre>'
                               "</div>", disponiveis, atual)

    piso = d.get("forca_minima", 0.15)
    lac = d.get("lacunas", [])

    if not lac:
        corpo = ('<div class="card"><p class="nada">Nenhuma lacuna nos últimos '
                 f'{d.get("dias", "?")} dias — toda pergunta recuperou nota forte '
                 'o bastante. Nada a escrever.</p></div>')
        triagem = ""
    else:
        pior = min((g["forca"] for g in lac if g["forca"] is not None), default=None)
        cabecalho = f"""
  <div class="card numeros">
    <div><b>{len(lac)}</b><span>lacuna(s)</span></div>
    <div><b>{d.get('assuntos', '?')}</b><span>assuntos perguntados</span></div>
    <div><b>{d.get('consultas_ok', '?')}</b><span>consultas de material</span></div>
    <div><b>{'—' if pior is None else f'{pior:.2f}'}</b><span>força mais fraca</span></div>
  </div>"""
        itens = []
        for i, g in enumerate(lac, 1):
            rotulo, classe = _MOTIVOS.get(g["motivo"], (g["motivo"], ""))
            itens.append(f"""
      <a class="item" href="#l{i}">
        <span class="num">{i}</span>
        <span class="txt">{_esc(g['pergunta'][:90])}</span>
        {_medidor(g['forca'], piso)}
        <span class="tag {classe}">{_esc(rotulo)}</span>
        <span class="rot sql-n">{g['respostas']} SQL</span>
      </a>""")
        triagem = cabecalho + '<div class="card triagem">' + "".join(itens) + "</div>"
        corpo = "".join(_bloco_lacuna(i, g, piso) for i, g in enumerate(lac, 1))

    try:
        with open(base + ".md", encoding="utf-8") as fh:
            markdown = fh.read()
    except OSError:
        markdown = ""

    return _moldura_dossie(atual, d.get("gerado_em", ""), triagem + corpo,
                           disponiveis, atual, markdown)


def _moldura_dossie(atual, gerado_em, corpo, disponiveis, marcado, markdown=""):
    abas = []
    for dt, _ in disponiveis[:12]:
        abas.append(f'<a class="{"on" if dt == marcado else ""}" '
                    f'href="/dossie?data={_esc(dt)}">{_esc(dt)}</a>')

    # O markdown inteiro viaja como JSON dentro de um <script>, e não como texto
    # escapado: dentro de <script> o navegador NÃO decodifica entidade, então
    # `_esc()` ali faria o botão copiar `&lt;banco&gt;` em vez de `<banco>` —
    # markdown quebrado, em silêncio. O `</` vira `<\/` para nenhuma SQL do
    # relatório conseguir fechar a tag antes da hora.
    caixa_md = ""
    if markdown:
        pacote = json.dumps(markdown, ensure_ascii=False).replace("</", "<\\/")
        caixa_md = (f'<script type="application/json" id="md">{pacote}</script>'
                    '<button class="primario" type="button" onclick="'
                    "copiar(JSON.parse(document.getElementById('md').textContent),this)\">"
                    "Copiar o relatório em markdown</button>")

    return HTMLResponse(f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Relatório de lacunas — {_esc(ENDPOINT)}</title>
<style>{CSS}{CSS_DOSSIE}</style></head>
<body><div class="wrap largo">
  <a class="link-topo" href="/">&#8592; Acessos</a>
  <a class="link-topo" href="/perguntas" style="margin-right:8px">Catálogo de perguntas</a>
  <h1>Relatório de lacunas</h1>
  <p class="sub">O que a base respondeu mal — com quem perguntou, as consultas que
     funcionaram e o rascunho de nota já montado.
     {f'Relatório de <strong>{_esc(atual)}</strong>' if atual else ''}
     {f'<span class="rot">· gerado {_esc(_quando(gerado_em))}</span>' if gerado_em else ''}</p>

  {f'<div class="filtros">{"".join(abas)}</div>' if len(abas) > 1 else ''}
  {f'<div class="acao-topo">{caixa_md}</div>' if caixa_md else ''}

  {corpo}

  <footer>Gerado pelo <code>gerar_dossie.sh</code> a partir de
  <code>perguntas/*.jsonl</code>, e publicado em
  <code>dossie/dossie-AAAA-MM-DD.{{json,md}}</code> — fora do git, como a trilha.
  <strong>É rascunho, não nota:</strong> a SQL registrada rodou, o que não quer
  dizer que a leitura dela estava certa. Confirme no banco antes de salvar em
  <code>NOTES/</code>.</footer>
</div><script>{JS}</script></body></html>""")


async def login(request: Request):
    if not SENHA:
        return RedirectResponse("/", status_code=303)
    erro = ""
    if request.method == "POST":
        form = await request.form()
        if hmac.compare_digest(str(form.get("senha", "")), SENHA):
            resp = RedirectResponse("/", status_code=303)
            exp = int(time.time()) + 8 * 3600
            resp.set_cookie(
                COOKIE, _assinar(exp), httponly=True, samesite="lax", max_age=8 * 3600
            )
            return resp
        # Atrasa a resposta: torna a força bruta inviável sem estado nenhum.
        time.sleep(1.0)
        erro = '<p class="sub" style="color:var(--erro)">Senha incorreta.</p>'
    return HTMLResponse(f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acessos do MCP — entrar</title><style>{CSS}</style></head>
<body><div class="wrap" style="max-width:420px">
  <h1>Acessos do MCP</h1>
  <div class="card">
    <form method="post" class="linha">
      <input type="password" name="senha" placeholder="Senha do painel" autofocus required>
      <button class="primario">Entrar</button>
    </form>
    {erro}
  </div>
</div></body></html>""")


async def sair(request: Request):
    resp = RedirectResponse("/login" if SENHA else "/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


async def _acao(request: Request, funcao):
    """Casca comum das ações: autenticação, CSRF, erro previsto -> tela."""
    if not _autenticado(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    if not _csrf_ok(form):
        return PlainTextResponse("CSRF inválido — recarregue a página.", status_code=400)
    # Publicado na rede, a pilha de chamada já não responde "quem revogou". Vale
    # por requisição (contextvar), não por processo.
    T.OPERADOR.set(_operador(request))
    try:
        dados = funcao(form)
    except T.ErroDeToken as e:
        dados = {"erro": str(e)}
    except Exception as e:  # não deixa a tela morrer por erro inesperado
        dados = {"erro": f"{e.__class__.__name__}: {e}"}
    return RedirectResponse(f"/?r={_guardar(**dados)}", status_code=303)


async def criar(request: Request):
    def faz(form):
        nome = str(form.get("nome", "")).strip()
        if not nome:
            raise T.ErroDeToken("informe o nome da pessoa")
        perfis = form.getlist("perfis")
        label, token = T.criar(nome, perfis)
        liberados = ", ".join(perfis) if perfis else "nenhum banco (libere na tabela)"
        return {
            "titulo": f"Token criado para {nome} (@{label}) — bancos: {liberados} "
                      f"· vence em {T.daqui()} ({T.PRAZO_DIAS} dias)",
            "token": token,
        }

    return await _acao(request, faz)


async def rotacionar(request: Request):
    def faz(form):
        exibicao, token = T.rotacionar(str(form.get("label", "")))
        return {"titulo": f"Token novo para {exibicao}", "token": token}

    return await _acao(request, faz)


async def perfis(request: Request):
    def faz(form):
        exibicao, aplicados = T.definir_perfis(
            str(form.get("label", "")), form.getlist("perfis")
        )
        quais = ", ".join(aplicados) if aplicados else "NENHUM banco"
        return {"mensagem": f"{exibicao} agora alcança: {quais}. Vale na próxima consulta dela."}

    return await _acao(request, faz)


def _pagina_confirmar_revogacao(p: T.Pessoa, erro: str = "") -> HTMLResponse:
    """Segundo passo da revogação: exige digitar o rótulo.

    O `confirm()` do navegador deixa a destruição a um clique de distância — e aqui
    ela derruba o acesso de uma pessoa na hora. Digitar o rótulo é o pedágio que
    separa intenção de acidente (mesmo padrão de "apagar repositório").
    """
    bancos = ", ".join(p.perfis) or "nenhum"
    aviso = f'<p class="sub" style="color:var(--erro)">{_esc(erro)}</p>' if erro else ""
    return HTMLResponse(f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Revogar acesso de {_esc(p.exibicao)}</title><style>{CSS}</style></head>
<body><div class="wrap" style="max-width:620px">
  <a class="link-topo" href="/">&#8592; Acessos</a>
  <h1>Revogar acesso</h1>
  <div class="card erro">
    <h2>{_esc(p.exibicao)}</h2>
    <p class="sub" style="margin:0">rótulo <code class="mono">@{_esc(p.label)}</code> ·
       bancos: {_esc(bancos)} · criado {_esc(p.criado) or '—'}</p>
    <p>O token dela para de funcionar <strong>imediatamente</strong> e o bloco sai do
       <code>tokens.caddy</code>. Para voltar atrás só dá gerando um token novo
       (ou restaurando um backup <code>tokens.caddy.bak-*</code> no servidor).</p>
    <form method="post" action="/revogar" class="linha">
      <input type="hidden" name="csrf" value="{_CSRF}">
      <input type="hidden" name="label" value="{_esc(p.label)}">
      <input type="text" name="confirmado" placeholder="digite {_esc(p.label)} para confirmar"
             autocomplete="off" autofocus required>
      <button class="primario">Revogar</button>
      <a class="link-topo" style="float:none" href="/">cancelar</a>
    </form>
    {aviso}
  </div>
</div></body></html>""")


async def revogar(request: Request):
    """Dois passos: sem `confirmado` correto, só mostra a tela de confirmação."""
    if not _autenticado(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    if not _csrf_ok(form):
        return PlainTextResponse("CSRF inválido — recarregue a página.", status_code=400)
    label = str(form.get("label", ""))
    try:
        alvo = next((p for p in T.com_uso(DIAS)[0] if p.label == label), None)
    except T.ErroDeToken as e:
        return RedirectResponse(f"/?r={_guardar(erro=str(e))}", status_code=303)
    if alvo is None:
        return RedirectResponse(
            f"/?r={_guardar(erro=f'não achei o acesso @{label}')}", status_code=303)

    digitado = str(form.get("confirmado", "")).strip()
    if not digitado:
        return _pagina_confirmar_revogacao(alvo)
    if digitado != alvo.label:
        return _pagina_confirmar_revogacao(
            alvo, f'"{digitado}" não é o rótulo. Digite exatamente {alvo.label}.')

    def faz(_form):
        exibicao = T.revogar(label)
        return {"mensagem": f"Acesso de {exibicao} revogado. O token não vale mais."}

    return await _acao(request, faz)


async def renovar(request: Request):
    def faz(form):
        exibicao, data = T.renovar(str(form.get("label", "")))
        return {"mensagem": f"Acesso de {exibicao} renovado até {data}. "
                            f"O token dela não mudou — nada a reconfigurar."}

    return await _acao(request, faz)


async def testar(request: Request):
    def faz(form):
        label = str(form.get("label", ""))
        pessoa = next((p for p in T.listar() if p.label == label), None)
        if pessoa is None:
            raise T.ErroDeToken(f"não achei o acesso @{label}")
        corpo = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "painel-acessos", "version": "1"}},
        }
        try:
            r = httpx.post(
                PROXY_INTERNO, json=corpo, timeout=20,
                headers={"Authorization": f"Bearer {pessoa.token}",
                         "Accept": "application/json, text/event-stream"},
            )
        except Exception as e:
            raise T.ErroDeToken(f"não alcancei o proxy em {PROXY_INTERNO}: {e}")
        if r.status_code == 401:
            raise T.ErroDeToken(
                f"o proxy RECUSOU o token de {pessoa.exibicao} (401) — "
                "o bloco dela não está valendo na configuração carregada"
            )
        if r.status_code != 200:
            raise T.ErroDeToken(f"o proxy respondeu HTTP {r.status_code}: {r.text[:200]}")
        return {"mensagem": (
            f"Token de {pessoa.exibicao} funcionando: o MCP respondeu ao handshake "
            f"(sessão {r.headers.get('mcp-session-id', '?')[:8]}…)."
        )}

    return await _acao(request, faz)


async def saude(request: Request):
    # SEM gate de propósito: é o que o healthcheck do container e o
    # watchdog_admin.sh consultam, e nenhum dos dois tem cookie. Mas o CÓDIGO é
    # que eles leem — o TEXTO só sai para quem está autenticado, desde que o
    # painel passou a responder na rede (a mensagem de degradação carrega caminho
    # interno, e a contagem de acessos diz o tamanho da operação).
    #
    # Só checagens passivas: NADA aqui pode recarregar o Caddy (um health check
    # que reconfigura o proxy seria uma arma apontada para a produção).
    problemas = []
    try:
        pessoas = T.listar()
    except Exception as e:
        pessoas = []
        problemas.append(f"tokens.caddy ilegível: {e}")
    if not os.path.exists(T.ADMIN_SOCK):
        problemas.append(f"socket de admin do Caddy ausente em {T.ADMIN_SOCK} (proxy fora do ar?)")
    detalhe = _autenticado(request) or _local(request)
    if problemas:
        corpo = "degradado: " + "; ".join(problemas) if detalhe else "degradado"
        return PlainTextResponse(corpo, status_code=503)
    corpo = f"ok — {len(pessoas)} acessos" if detalhe else "ok"
    return PlainTextResponse(corpo, status_code=200)


app = Starlette(routes=[
    Route("/", raiz),
    Route("/comandos", comandos),
    Route("/consultas", consultas),
    Route("/perguntas", catalogo_perguntas),
    Route("/dossie", dossie),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/sair", sair, methods=["POST"]),
    Route("/criar", criar, methods=["POST"]),
    Route("/rotacionar", rotacionar, methods=["POST"]),
    Route("/revogar", revogar, methods=["POST"]),
    Route("/perfis", perfis, methods=["POST"]),
    Route("/renovar", renovar, methods=["POST"]),
    Route("/testar", testar, methods=["POST"]),
    Route("/saude", saude),
])


if __name__ == "__main__":
    # Fail closed: sem senha, este painel só existe em loopback. Ele cria
    # credenciais de acesso aos bancos; publicá-lo na rede sem login não é uma
    # opção que o código oferece.
    if BIND not in ("127.0.0.1", "localhost", "::1") and not SENHA:
        print(
            f"ERRO: ADMIN_BIND={BIND} expõe o painel fora do loopback sem login. "
            "Defina ADMIN_SENHA no .env.admin (ou volte o bind para 127.0.0.1).",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Painel de acessos do MCP em :{PORTA} (publicado em {BIND})", file=sys.stderr)
    if not T.PERFIS:
        print("[aviso] nenhum perfil em PERFIS: a tela não terá banco para liberar. "
              "Confira o .env (o compose repassa PERFIS ao painel).", file=sys.stderr)
    # access_log LIGADO: sem ele não há como saber se um POST chegou nem de
    # onde — e "quem mexeu no arquivo de tokens" fica sem resposta.
    uvicorn.run(app, host="0.0.0.0", port=PORTA, access_log=True)
