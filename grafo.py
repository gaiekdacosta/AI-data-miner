#!/usr/bin/env python3
"""Grafo de conhecimento das NOTES — índice JSON + arestas, e consultas.

Transforma as anotações (``NOTES/<banco>/<assunto>/*.md``) num grafo navegável
**sem banco nenhum**:

  - nós  ``nota``   — uma por arquivo em NOTES/ (varredura recursiva)
  - nós  ``tabela`` — tabelas/views citadas (validadas contra os dicionários)
  - arestas:
      nota  --cita-->     tabela   (frontmatter ``tabelas:`` + nomes no corpo)
      nota  --referencia--> nota    (wikilinks ``[[slug]]``)
      tabela --ponte--> tabela      (frontmatter ``pontes: - A -> B (...)``)

Materializa dois arquivos determinísticos (git-friendly); a fonte da verdade
continua sendo o markdown:

  contexto/notes.json  — índice/manifesto (1 entrada por nota, com atributos)
  contexto/edges.json  — nós + arestas (formato carregável no NetworkX)

Uso:

    python3 grafo.py construir                     # (re)gera notes.json + edges.json
    python3 grafo.py vizinhos PEDIDOS              # vizinhos diretos de um nó
    python3 grafo.py vizinhos PEDIDOS --hops 2     # até 2 saltos
    python3 grafo.py caminho CLIENTES PRODUTOS     # menor caminho entre 2 nós
    python3 grafo.py notas-de ITENS_PEDIDO         # notas que citam uma tabela
    python3 grafo.py tabelas-de faturamento-so-pedido-faturado
    python3 grafo.py orfas                         # notas sem nenhuma aresta

Carregar no NetworkX depois, se quiser (não é dependência aqui):

    import json, networkx as nx
    g = nx.node_link_graph(json.load(open("contexto/edges.json")), edges="edges")
"""

import json
import os
import re
import sys
from collections import deque

_AQUI = os.path.dirname(os.path.abspath(__file__))
_NOTES = os.path.join(_AQUI, "NOTES")
_CONTEXTO = os.path.join(_AQUI, "contexto")
_NOTES_JSON = os.path.join(_CONTEXTO, "notes.json")
_EDGES_JSON = os.path.join(_CONTEXTO, "edges.json")

# Identificador candidato a nome de tabela no corpo da nota. Só vira aresta se o
# nome existir em algum dicionário — é o dicionário que separa "pedidos" (tabela)
# de "pedidos" (palavra). Sem dicionário nenhum, só o frontmatter conta.
_RE_IDENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_$#]{2,})\b")
_RE_LINK = re.compile(r"\[\[([^\]]+)\]\]")
_RE_DICT_TAB = re.compile(r"^### ([A-Za-z0-9_$#]+) \(", re.MULTILINE)
# "PEDIDOS -> ITENS_PEDIDO (pedido_id)" -> ("PEDIDOS", "ITENS_PEDIDO")
_RE_PONTE = re.compile(r"([A-Za-z0-9_$#]+)\s*->\s*([A-Za-z0-9_$#]+)")
_RE_ARQ_DICT = re.compile(r"^dicionario_([a-z][a-z0-9_]*)\.md$")


# ----------------------------------------------------------------------------
# Dicionários
# ----------------------------------------------------------------------------
def dicionarios():
    """{banco: caminho} de cada ``contexto/dicionario_<banco>.md``, em ordem.

    É o único lugar que sabe o padrão de nome: ``vetorial``, ``recuperar``,
    ``validar_sql`` e ``check`` perguntam aqui. Um banco existe para a base de
    conhecimento quando o dicionário dele foi gerado.
    """
    try:
        nomes = sorted(os.listdir(_CONTEXTO))
    except OSError:
        return {}
    saida = {}
    for nome in nomes:
        m = _RE_ARQ_DICT.match(nome)
        if m:
            saida[m.group(1)] = os.path.join(_CONTEXTO, nome)
    return saida


def _tabelas_validas():
    """Tabelas/views reais (MAIÚSCULAS), lidas dos cabeçalhos de TODOS os dicionários.

    Serve para descartar falso positivo no corpo da nota: o dicionário só contém o
    que existe de fato no banco. Sem dicionário nenhum, devolve None (e aí só o
    frontmatter conta).
    """
    caminhos = list(dicionarios().values())
    if not caminhos:
        return None
    validas = set()
    for caminho in caminhos:
        with open(caminho, encoding="utf-8") as fh:
            validas.update(t.upper() for t in _RE_DICT_TAB.findall(fh.read()))
    return validas


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------
def _parse_frontmatter(texto):
    """Extrai o frontmatter YAML-simples e devolve (dados, corpo).

    Só lida com o que precisamos: chaves de topo, listas inline ``[a, b]`` e
    listas em bloco ``- item``. Blocos aninhados são ignorados de propósito.
    """
    if not texto.startswith("---"):
        return {}, texto
    linhas = texto.splitlines()
    fim = next((i for i in range(1, len(linhas)) if linhas[i].strip() == "---"), None)
    if fim is None:
        return {}, texto
    corpo = "\n".join(linhas[fim + 1:])

    dados = {}
    chave_atual = None
    for ln in linhas[1:fim]:
        if not ln.strip():
            continue
        m = re.match(r"^(\s*)-\s+(.*)$", ln)          # item de lista em bloco
        if m and chave_atual:
            dados.setdefault(chave_atual, [])
            if isinstance(dados[chave_atual], list):
                dados[chave_atual].append(m.group(2).strip())
            continue
        m = re.match(r"^(\s*)([A-Za-z_][\w]*):\s*(.*)$", ln)
        if not m:
            continue
        indent, chave, val = m.group(1), m.group(2), m.group(3).strip()
        if indent:                                    # aninhado -> ignora
            continue
        if val == "":
            chave_atual = chave
            dados.setdefault(chave, [])
        elif val.startswith("[") and val.endswith("]"):
            dados[chave] = [x.strip().strip("\"'") for x in val[1:-1].split(",") if x.strip()]
            chave_atual = None
        else:
            dados[chave] = val.strip().strip("\"'")
            chave_atual = None
    return dados, corpo


def ler_notas(raiz=None):
    """Percorre NOTES/ recursivamente e devolve uma entrada por nota.

    É o único lugar que sabe varrer a pasta — ``vetorial.py``, ``check.py`` e
    ``gerar_dicionario.py`` chamam esta função para não divergirem no que conta
    como nota (``_TEMPLATE.md`` e ``_archive/`` ficam fora).

    Cada entrada traz ``banco``/``assunto`` derivados do CAMINHO
    (``NOTES/<banco>/<assunto>/nota.md``) — a organização visível — e o
    ``frontmatter``, onde ``perfil`` deve concordar com o ``banco`` (o check.py
    cobra isso).
    """
    base = raiz or _NOTES
    achadas = []
    for pasta, dirs, arqs in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d != "_archive")
        for nome in sorted(arqs):
            if not nome.lower().endswith(".md") or nome.startswith("_"):
                continue
            caminho = os.path.join(pasta, nome)
            rel = os.path.relpath(caminho, _AQUI).replace(os.sep, "/")
            partes = os.path.relpath(caminho, base).replace(os.sep, "/").split("/")
            with open(caminho, encoding="utf-8") as fh:
                texto = fh.read()
            fm, corpo = _parse_frontmatter(texto)
            achadas.append({
                "id": fm.get("name") or nome[:-3],
                "arquivo": rel,
                "banco": partes[0] if len(partes) > 1 else "",
                "assunto": partes[1] if len(partes) > 2 else "",
                "frontmatter": fm,
                "corpo": corpo,
                "texto": texto,
            })
    achadas.sort(key=lambda n: n["arquivo"])
    return achadas


# ----------------------------------------------------------------------------
# Construção do grafo
# ----------------------------------------------------------------------------
def construir(tabelas_validas=None, verbose=True):
    """Lê NOTES/, monta os nós/arestas e grava notes.json + edges.json."""
    if tabelas_validas is None:
        tabelas_validas = _tabelas_validas()
    else:
        tabelas_validas = {t.upper() for t in tabelas_validas}

    notas = []
    nos = {}          # id -> tipo
    arestas = set()   # (source, target, tipo)

    def _add_no(nid, tipo):
        nos.setdefault(nid, tipo)

    if not os.path.isdir(_NOTES):
        raise SystemExit(f"pasta NOTES não encontrada: {_NOTES}")

    for nota in ler_notas():
        fm, corpo = nota["frontmatter"], nota["corpo"]
        nid = nota["id"]
        _add_no(nid, "nota")

        # Tabelas: frontmatter (confiável) + nomes no corpo, só se existirem num
        # dicionário (sem dicionário, o corpo não conta).
        tabs = set(t.upper() for t in fm.get("tabelas", []) if isinstance(t, str))
        if tabelas_validas:
            for cand in _RE_IDENT.findall(corpo):
                if cand.upper() in tabelas_validas:
                    tabs.add(cand.upper())
        for t in sorted(tabs):
            _add_no(t, "tabela")
            arestas.add((nid, t, "cita"))

        # Wikilinks: nota -> nota.
        links = sorted({l.strip() for l in _RE_LINK.findall(corpo)})
        for l in links:
            _add_no(l, "nota")
            arestas.add((nid, l, "referencia"))

        # Pontes explícitas: tabela -> tabela.
        pontes = []
        for p in fm.get("pontes", []):
            m = _RE_PONTE.search(p if isinstance(p, str) else "")
            if not m:
                continue
            a, b = m.group(1).upper(), m.group(2).upper()
            _add_no(a, "tabela")
            _add_no(b, "tabela")
            arestas.add((a, b, "ponte"))
            pontes.append(f"{a} -> {b}")

        notas.append({
            "id": nid,
            "titulo": fm.get("titulo") or fm.get("description") or nid,
            "perfil": fm.get("perfil"),
            "tabelas": sorted(tabs),
            "tags": fm.get("tags") or fm.get("conceitos") or [],
            "links": links,
            "pontes": pontes,
            "arquivo": nota["arquivo"],
            "assunto": nota["assunto"],
        })

    os.makedirs(os.path.dirname(_NOTES_JSON), exist_ok=True)
    with open(_NOTES_JSON, "w", encoding="utf-8") as fh:
        json.dump({"notas": notas}, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")

    nodes = [{"id": nid, "tipo": t} for nid, t in sorted(nos.items())]
    edges = [{"source": s, "target": d, "tipo": tp}
             for s, d, tp in sorted(arestas)]
    with open(_EDGES_JSON, "w", encoding="utf-8") as fh:
        json.dump({"directed": True, "multigraph": True, "graph": {},
                   "nodes": nodes, "edges": edges},
                  fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")

    if verbose:
        n_tab = sum(1 for t in nos.values() if t == "tabela")
        print(f"grafo: {len(notas)} notas, {n_tab} tabelas, {len(arestas)} arestas")
        print(f"  -> {_NOTES_JSON}")
        print(f"  -> {_EDGES_JSON}")
    return notas, nodes, edges


# ----------------------------------------------------------------------------
# Consultas (Python puro — BFS sobre edges.json)
# ----------------------------------------------------------------------------
def _carregar():
    if not os.path.isfile(_EDGES_JSON):
        raise SystemExit("edges.json não existe — rode antes: python3 grafo.py construir")
    with open(_EDGES_JSON, encoding="utf-8") as fh:
        g = json.load(fh)
    tipo = {n["id"]: n["tipo"] for n in g["nodes"]}
    adj = {n["id"]: [] for n in g["nodes"]}          # não-direcionado p/ navegar
    rotulo = {}
    for e in g["edges"]:
        adj.setdefault(e["source"], []).append((e["target"], e["tipo"]))
        adj.setdefault(e["target"], []).append((e["source"], e["tipo"]))
        rotulo[(e["source"], e["target"])] = e["tipo"]
        rotulo[(e["target"], e["source"])] = e["tipo"]
    return tipo, adj, rotulo


def _resolver(no, tipo):
    """Aceita o nó exato (case-insensitive p/ tabelas)."""
    if no in tipo:
        return no
    up = no.upper()
    if up in tipo:
        return up
    raise SystemExit(f"nó não encontrado: {no!r}")


def vizinhos(no, hops=1):
    tipo, adj, _ = _carregar()
    ini = _resolver(no, tipo)
    dist = {ini: 0}
    fila = deque([ini])
    while fila:
        atual = fila.popleft()
        if dist[atual] >= hops:
            continue
        for viz, _ in adj.get(atual, []):
            if viz not in dist:
                dist[viz] = dist[atual] + 1
                fila.append(viz)
    itens = sorted(((d, tipo.get(v, "?"), v) for v, d in dist.items() if v != ini))
    print(f"Vizinhos de {ini} (até {hops} salto(s)): {len(itens)}")
    for d, tp, v in itens:
        print(f"  [{d}] {tp:7} {v}")
    return itens


def caminho(a, b):
    tipo, adj, rotulo = _carregar()
    ini, fim = _resolver(a, tipo), _resolver(b, tipo)
    prev = {ini: None}
    fila = deque([ini])
    while fila:
        atual = fila.popleft()
        if atual == fim:
            break
        for viz, _ in adj.get(atual, []):
            if viz not in prev:
                prev[viz] = atual
                fila.append(viz)
    if fim not in prev:
        print(f"Sem caminho entre {ini} e {fim}.")
        return None
    seq = []
    cur = fim
    while cur is not None:
        seq.append(cur)
        cur = prev[cur]
    seq.reverse()
    print(f"Caminho {ini} -> {fim} ({len(seq) - 1} aresta(s)):")
    for i, no in enumerate(seq):
        if i == 0:
            print(f"  {no}")
        else:
            print(f"    --{rotulo.get((seq[i-1], no), '?')}--> {no}")
    return seq


def notas_de(tabela):
    with open(_NOTES_JSON, encoding="utf-8") as fh:
        notas = json.load(fh)["notas"]
    up = tabela.upper()
    achadas = [n for n in notas if up in n["tabelas"]]
    print(f"Notas que citam {up}: {len(achadas)}")
    for n in achadas:
        print(f"  {n['id']}  ({n['arquivo']})")
    return achadas


def tabelas_de(nota):
    with open(_NOTES_JSON, encoding="utf-8") as fh:
        notas = json.load(fh)["notas"]
    achada = next((n for n in notas if n["id"] == nota), None)
    if not achada:
        raise SystemExit(f"nota não encontrada: {nota!r}")
    print(f"{nota} cita {len(achada['tabelas'])} tabela(s): {', '.join(achada['tabelas']) or '(nenhuma)'}")
    if achada["links"]:
        print(f"  referencia: {', '.join(achada['links'])}")
    if achada["pontes"]:
        print(f"  pontes: {'; '.join(achada['pontes'])}")
    return achada


def orfas():
    tipo, adj, _ = _carregar()
    soltas = sorted(n for n, t in tipo.items() if t == "nota" and not adj.get(n))
    print(f"Notas órfãs (sem tabela citada nem link): {len(soltas)}")
    for n in soltas:
        print(f"  {n}")
    return soltas


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 0
    cmd, resto = argv[0], argv[1:]
    if cmd == "construir":
        construir()
    elif cmd == "vizinhos":
        hops = 1
        if "--hops" in resto:
            i = resto.index("--hops")
            hops = int(resto[i + 1])
            resto = resto[:i] + resto[i + 2:]
        vizinhos(resto[0], hops)
    elif cmd == "caminho":
        caminho(resto[0], resto[1])
    elif cmd in ("notas-de", "notas_de"):
        notas_de(resto[0])
    elif cmd in ("tabelas-de", "tabelas_de"):
        tabelas_de(resto[0])
    elif cmd == "orfas":
        orfas()
    else:
        print(f"comando desconhecido: {cmd}\n")
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
