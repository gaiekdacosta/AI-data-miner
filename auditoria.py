#!/usr/bin/env python3
"""Trilha de auditoria das consultas do MCP — quem consultou o quê, quando.

O proxy registra *que* houve uma chamada e *de quem* (``log_append pessoa``), mas
não **o que** foi consultado: o corpo é JSON-RPC dentro de um POST /mcp. Sem isto,
"quem leu o banco de RH" não tem resposta — e é justamente o que o gate de banco
por pessoa exige poder responder.

Um arquivo JSON Lines por dia (``consultas-AAAA-MM-DD.jsonl``), append-only:

    {"ts":"2026-01-15T14:02:11", "pessoa":"maria_silva", "perfil":"vendas",
     "tool":"consultar_sql", "ok":true, "linhas":37, "truncado":false,
     "ms":412, "tabelas":["PEDIDOS","CLIENTES"], "sql":"SELECT ..."}

Decisões:

* **Nunca derruba a consulta.** Qualquer falha ao gravar é engolida (com um aviso
  único no stderr): auditoria quebrada não pode virar indisponibilidade.
* **Registra a recusa também** (``ok:false, motivo:"perfil_negado"``). Tentativa
  barrada é o evento mais interessante de todos para quem audita.
* **Grava o SQL inteiro** (até um teto). É o ponto: sem o texto da consulta, saber
  que "alguém rodou algo no banco de RH" não serve para nada. Consequência: o
  arquivo contém dado sensível (um WHERE pode ter CPF) — fica fora do git, 0600,
  com o mesmo cuidado do ``.env``.
* **Um arquivo por dia** em vez de rotação por tamanho: retenção e expurgo viram
  ``rm`` de arquivos antigos, e o painel lê só os dias que precisa.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime

DIR = os.environ.get("AUDIT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "auditoria"))
MAX_SQL = int(os.environ.get("AUDIT_MAX_SQL", "4000"))

_TRAVA = threading.Lock()   # o servidor MCP atende em paralelo: uma linha por vez
_avisou = False

# Tabelas citadas na consulta — o que dá para filtrar depois por "quem leu X".
_RE_FROM_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+(?:dbo\.)?([A-Za-z_][\w$#]*)", re.I)


def tabelas_do_sql(sql: str) -> list:
    if not sql:
        return []
    vistas = []
    for nome in _RE_FROM_JOIN.findall(sql):
        up = nome.upper()
        if up not in vistas and up not in ("SELECT", "DUAL", "TABLE"):
            vistas.append(up)
    return vistas[:12]


def _arquivo(quando: datetime) -> str:
    return os.path.join(DIR, f"consultas-{quando:%Y-%m-%d}.jsonl")


def registrar(pessoa, perfil, tool, ok=True, sql=None, linhas=None,
              truncado=None, ms=None, motivo=None, erro=None) -> None:
    """Acrescenta uma linha à trilha. Silencioso em caso de falha, de propósito."""
    global _avisou
    agora = datetime.now()
    registro = {
        "ts": agora.isoformat(timespec="seconds"),
        "pessoa": pessoa or "?",
        "perfil": perfil,
        "tool": tool,
        "ok": bool(ok),
    }
    if sql:
        registro["sql"] = sql[:MAX_SQL]
        registro["tabelas"] = tabelas_do_sql(sql)
    if linhas is not None:
        registro["linhas"] = linhas
    if truncado is not None:
        registro["truncado"] = bool(truncado)
    if ms is not None:
        registro["ms"] = int(ms)
    if motivo:
        registro["motivo"] = motivo
    if erro:
        registro["erro"] = str(erro)[:400]

    try:
        with _TRAVA:
            os.makedirs(DIR, mode=0o750, exist_ok=True)
            caminho = _arquivo(agora)
            novo = not os.path.exists(caminho)
            with open(caminho, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
            if novo:  # o arquivo tem SQL — e SQL às vezes tem CPF
                os.chmod(caminho, 0o600)
    except Exception as e:  # auditoria nunca derruba consulta
        if not _avisou:
            import sys
            print(f"[auditoria] não consegui gravar em {DIR}: {e}", file=sys.stderr)
            _avisou = True


def ler(dias=7, limite=300, pessoa=None):
    """Últimos registros, do mais recente para o mais antigo."""
    from datetime import timedelta
    saida = []
    hoje = datetime.now()
    for i in range(dias):
        caminho = _arquivo(hoje - timedelta(days=i))
        if not os.path.isfile(caminho):
            continue
        try:
            with open(caminho, encoding="utf-8") as fh:
                linhas = fh.readlines()
        except OSError:
            continue
        for linha in reversed(linhas):
            linha = linha.strip()
            if not linha:
                continue
            try:
                reg = json.loads(linha)
            except ValueError:
                continue
            if pessoa and reg.get("pessoa") != pessoa:
                continue
            saida.append(reg)
            if len(saida) >= limite:
                return saida
    return saida


def resumo(dias=7):
    """{pessoa: {consultas, negadas, linhas, ultimo}} para a tela."""
    por_pessoa = {}
    for reg in ler(dias=dias, limite=100000):
        d = por_pessoa.setdefault(reg.get("pessoa", "?"),
                                 {"consultas": 0, "negadas": 0, "linhas": 0, "ultimo": ""})
        d["consultas"] += 1
        if not reg.get("ok"):
            d["negadas"] += 1
        d["linhas"] += reg.get("linhas") or 0
        if reg["ts"] > d["ultimo"]:
            d["ultimo"] = reg["ts"]
    return por_pessoa
