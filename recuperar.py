#!/usr/bin/env python3
"""Recuperação híbrida (léxico + grafo) do conhecimento para uma pergunta.

Em vez de despejar os dicionários inteiros no prompt do agente (num ERP, centenas
de milhares de tokens), seleciona só o **subgrafo relevante** à pergunta:

  1. sementes pelo índice vetorial — ``vetorial.py`` diz quais NOTES/tabelas
     estão mais próximas da pergunta (TF-IDF em contexto/vetores.json) e onde
     elas moram; sem o arquivo, cai no léxico simples (overlap de radicais);
  2. expansão pelo grafo  — a partir das sementes, puxa tabelas vizinhas
     (arestas ``ponte``), tabelas citadas pelas notas e notas ligadas
     (``[[links]]``), tudo lido de contexto/edges.json;
  3. renderização         — monta um markdown enxuto: o bloco de schema de cada
     tabela (recortado do dicionário do banco dela) + o corpo das NOTES escolhidas.

Tudo em Python puro, lendo contexto/{vetores,notes,edges}.json + dicionários —
sem banco e sem dependências. Trocar o TF-IDF por embeddings de verdade mexe só
na etapa 1 (``vetorial.buscar``); o resto (grafo + render) não muda.

Uso (teste rápido, sem API):

    python3 recuperar.py "faturamento por categoria no mês passado"
    python3 recuperar.py --indice          # imprime o índice global compacto
"""

import json
import os
import re
import sys

try:                      # importado como pacote (mcp_server) ou script solto
    from . import vetorial
    from . import perfis as _perfis
except ImportError:
    import vetorial
    import perfis as _perfis

_AQUI = os.path.dirname(os.path.abspath(__file__))
_NOTES_JSON = os.path.join(_AQUI, "contexto", "notes.json")
_EDGES_JSON = os.path.join(_AQUI, "contexto", "edges.json")

# Léxico (stopwords, acento, radical) vive no vetorial.py: é o mesmo usado para
# construir os vetores — divergir aqui faria a busca de reserva pontuar diferente
# do índice.
_STOP = vetorial._STOP
_norm = vetorial._norm


def _rotulo(banco):
    """Como o banco aparece para o modelo: os PERFIS que o leem.

    É o que diz ao modelo qual ``perfil`` passar em ``consultar_sql``. Dois perfis
    no mesmo banco de conhecimento aparecem juntos (``erp/erp_bi``); sem .env
    (ferramenta rodando solta), cai no nome do banco.
    """
    quem = _perfis.perfis_do_banco(banco)
    return "/".join(quem) if quem else banco


def _tokset(texto):
    """Conjunto de tokens normalizados (>=3 letras, sem stopwords)."""
    return {t for t in re.split(r"[^0-9a-z]+", _norm(texto))
            if len(t) >= 3 and t not in _STOP}


def _match(q, alvo_tokens):
    """q casa com o alvo se compartilha radical (prefixo comum >= 4) com alguma
    palavra — assim plural/singular/gênero se unificam (pedidos~pedido)."""
    for t in alvo_tokens:
        if q == t or len(os.path.commonprefix((q, t))) >= 4:
            return True
    return False


def _score(consulta_tokens, texto):
    alvo = _tokset(texto)
    return sum(1 for q in consulta_tokens if _match(q, alvo))


# ---------------------------------------------------------------------------
# Carregamento (lazy, cacheado em módulo)
# ---------------------------------------------------------------------------
_ESTADO = None


def _dividir_blocos_schema(texto_dic):
    """Parseia o dicionário em {TAB: [(coluna, tipo, comentário)]},
    {TAB: comentário-da-tabela} e {TAB: nome-real}. Para na seção de NOTES.

    A chave é sempre MAIÚSCULA (é o que unifica a mesma tabela citada em caixas
    diferentes e o que o ``schema_tabela`` recebe), mas o nome real vai junto: num
    MySQL com ``lower_case_table_names=0`` o servidor é case-sensitive, e mostrar
    a chave ao modelo geraria SQL que o banco recusa.
    """
    colunas, comentarios, nomes = {}, {}, {}
    atual = None
    for linha in texto_dic.splitlines():
        if linha.startswith("## Conhecimento"):
            break
        m = re.match(r"^###\s+([A-Za-z0-9_$#]+)\s+\((?:TABLE|VIEW)\)\s*(?:—\s*(.*))?$", linha)
        if m:
            atual = m.group(1).upper()
            nomes[atual] = m.group(1)
            comentarios[atual] = (m.group(2) or "").strip()
            colunas[atual] = []
            continue
        if atual and linha.startswith("|"):
            cel = [c.strip() for c in linha.strip().strip("|").split("|")]
            if len(cel) < 2 or cel[0] == "Coluna" or set(cel[0]) <= set("-: "):
                continue
            com = cel[3] if len(cel) > 3 else ""
            colunas[atual].append((cel[0], cel[1], com))
    return colunas, comentarios, nomes


def _corpo_nota(caminho_rel):
    """Texto da nota sem o frontmatter (só o conhecimento)."""
    caminho = os.path.join(_AQUI, caminho_rel)
    if not os.path.isfile(caminho):
        return ""
    with open(caminho, encoding="utf-8") as fh:
        texto = fh.read()
    if texto.startswith("---"):
        partes = texto.split("---", 2)
        if len(partes) == 3:
            return partes[2].strip()
    return texto.strip()


def _carregar():
    global _ESTADO
    if _ESTADO is not None:
        return _ESTADO
    with open(_NOTES_JSON, encoding="utf-8") as fh:
        notas = {n["id"]: n for n in json.load(fh)["notas"]}
    with open(_EDGES_JSON, encoding="utf-8") as fh:
        g = json.load(fh)
    # Um esquema POR BANCO, nunca mesclado: a mesma sigla de tabela pode existir
    # em dois bancos com colunas diferentes, e mesclar entregaria o schema errado
    # ao modelo sem ninguém perceber.
    esquemas = {}
    for banco, caminho in vetorial.dicionarios().items():
        with open(caminho, encoding="utf-8") as fh:
            esquemas[banco] = _dividir_blocos_schema(fh.read())
    # tabela -> bancos onde ela existe (na ordem dos dicionários)
    onde = {}
    for banco in esquemas:
        for tab in esquemas[banco][0]:
            onde.setdefault(tab, []).append(banco)

    # adjacências (não-direcionadas) por tipo de aresta
    ponte = {}       # tabela -> {tabelas vizinhas}
    cita = {}        # nota -> {tabelas}   e  tabela -> {notas}
    ref = {}         # nota -> {notas}
    for e in g["edges"]:
        s, d, tp = e["source"], e["target"], e["tipo"]
        if tp == "ponte":
            ponte.setdefault(s, set()).add(d)
            ponte.setdefault(d, set()).add(s)
        elif tp == "cita":
            cita.setdefault(s, set()).add(d)
            cita.setdefault(d, set()).add(s)
        elif tp == "referencia":
            ref.setdefault(s, set()).add(d)
            ref.setdefault(d, set()).add(s)

    _ESTADO = {
        "notas": notas, "esquemas": esquemas, "onde": onde,
        "ponte": ponte, "cita": cita, "ref": ref,
    }
    return _ESTADO


def disponivel():
    """O índice da base existe? Dicionário é opcional: sem ele, só notas."""
    return all(os.path.isfile(p) for p in (_NOTES_JSON, _EDGES_JSON))


# ---------------------------------------------------------------------------
# Índice global compacto
# ---------------------------------------------------------------------------
def indice_global():
    st = _carregar()
    linhas = ["## Índice — tabelas conhecidas (nome [perfil] — comentário)"]
    for tab in sorted(st["onde"]):
        bancos = st["onde"][tab]
        com = next((st["esquemas"][b][1].get(tab, "") for b in bancos
                    if st["esquemas"][b][1].get(tab)), "")
        rot = " | ".join(_rotulo(b) for b in bancos)
        # Nome REAL (o do primeiro banco onde a tabela aparece): é dele que o
        # modelo copia o identificador para dentro do SELECT.
        nome = st["esquemas"][bancos[0]][2].get(tab, tab)
        linhas.append(f"- {nome} [{rot}]{(' — ' + com) if com else ''}")
    linhas.append("\n## Índice — NOTES (regras/armadilhas registradas)")
    # Ordenado pelo CAMINHO: as notas saem agrupadas por banco/assunto, que é a
    # organização da pasta — e o caminho é o que se abre para ler a nota inteira.
    for nid in sorted(st["notas"], key=lambda i: st["notas"][i].get("arquivo") or i):
        n = st["notas"][nid]
        titulo = (n.get("titulo") or nid)[:140]
        perfil = f" [{n['perfil']}]" if n.get("perfil") else ""
        onde = f" ({n['arquivo']})" if n.get("arquivo") else ""
        linhas.append(f"- {nid}{perfil}{onde}: {titulo}")
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Recuperação híbrida
# ---------------------------------------------------------------------------
def _sementes_lexico(st, pergunta, max_notas):
    """Reserva: pontua por overlap de radicais, sem o arquivo vetorial."""
    toks = _tokset(pergunta)
    if not toks:
        return [], []
    notas_pont = []
    for nid, n in st["notas"].items():
        alvo = " ".join([nid.replace("-", " "), n.get("titulo", ""),
                         " ".join(n.get("tabelas", [])), " ".join(n.get("tags", []))])
        s = _score(toks, alvo)
        if s:
            notas_pont.append((s, nid))
    notas_pont.sort(key=lambda x: (-x[0], x[1]))

    tab_pont = []
    for tab, bancos in st["onde"].items():
        com = " ".join(st["esquemas"][b][1].get(tab, "") for b in bancos)
        s = _score(toks, tab + " " + com)
        if s:
            tab_pont.append((s, tab))
    tab_pont.sort(key=lambda x: (-x[0], x[1]))
    return [nid for _, nid in notas_pont[:max_notas]], [t for _, t in tab_pont]


def _corte(achados, fracao=0.30, piso=0.05):
    """Descarta a cauda fraca do ranking.

    O cosseno sempre devolve algo: numa pergunta de vendas, uma nota de cadastro
    pode pontuar 0,07 contra 0,28 da certa. Levar essa cauda para o prompt gasta
    tokens e ainda sugere tabela de outro assunto — então só passa quem chega perto
    do melhor resultado (e acima de um piso absoluto, para pergunta vaga).
    """
    if not achados:
        return []
    teto = achados[0]["score"]
    return [a for a in achados if a["score"] >= max(piso, teto * fracao)]


def forca(pergunta):
    """Quão bem a base cobre esta pergunta — o melhor score de nota (0..1).

    É o mesmo número que ``_sementes`` usa para escolher a semente, exposto para o
    catálogo de perguntas (``perguntas.py``). Sem ele, "recuperou" é booleano e
    vale sempre ``True`` (o cosseno devolve alguma nota para qualquer texto), e a
    fila de lacunas nunca dispara. Com ele, "recuperou fraco" vira ordenável.

    Zero quando não há índice vetorial (o léxico de reserva não pontua na mesma
    escala e comparar os dois daria fila falsa).
    """
    if not vetorial.disponivel():
        return 0.0
    try:
        achadas = vetorial.buscar(pergunta, n=1, tipo="nota")
    except Exception:
        return 0.0
    return round(achadas[0]["score"], 4) if achadas else 0.0


def _sementes(st, pergunta, max_notas, max_tabelas):
    """Notas e tabelas mais próximas da pergunta — o ponto de partida do subgrafo.

    Vem do índice vetorial (contexto/vetores.json), que casa abreviação com
    palavra inteira e pesa o termo raro acima do comum. Se o arquivo não existe ou
    está ilegível, volta ao léxico: recuperar de menos é aceitável, não recuperar
    nada não é.
    """
    if vetorial.disponivel():
        try:
            achadas = _corte(vetorial.buscar(pergunta, n=max_notas, tipo="nota"))
            notas = [a["id"] for a in achadas if a["id"] in st["notas"]]
            tabs = []
            for a in _corte(vetorial.buscar(pergunta, n=max_tabelas * 3,
                                            tipo="tabela")):
                tab = a["id"].split(":", 1)[-1]      # id é "banco:TABELA"
                if tab not in tabs:
                    tabs.append(tab)
            if notas or tabs:
                return notas, tabs
        except Exception as exc:                     # índice velho não derruba a consulta
            print(f"(índice vetorial ignorado: {exc})", file=sys.stderr)
    return _sementes_lexico(st, pergunta, max_notas)


def _selecionar(pergunta, max_tabelas=10, max_notas=5, hops=1):
    st = _carregar()
    seed_notas, seed_tabs = _sementes(st, pergunta, max_notas, max_tabelas)
    if not seed_notas and not seed_tabs:
        return [], []

    # (2) expansão pelo grafo
    sel_notas = list(dict.fromkeys(seed_notas))
    for nid in seed_notas:                       # notas ligadas por [[link]]
        for viz in sorted(st["ref"].get(nid, ())):
            if viz in st["notas"] and viz not in sel_notas:  # só notas reais
                sel_notas.append(viz)
    sel_notas = [n for n in sel_notas if n in st["notas"]][:max_notas]

    # Filtro por `onde` (qualquer banco), e não pelo primeiro dicionário: tabela
    # que só existe num banco secundário não pode ser descartada aqui — o modelo
    # receberia a regra da nota sem schema nenhum e inventaria nome de coluna.
    sel_tabs = [t for t in dict.fromkeys(seed_tabs) if t in st["onde"]]
    for nid in sel_notas:                        # tabelas citadas pelas notas
        for t in sorted(st["cita"].get(nid, ())):
            if t in st["onde"] and t not in sel_tabs:
                sel_tabs.append(t)
    if hops >= 1:                                # tabelas-ponte a 1 salto
        for t in list(sel_tabs):
            for viz in sorted(st["ponte"].get(t, ())):
                if viz in st["onde"] and viz not in sel_tabs:
                    sel_tabs.append(viz)
    sel_tabs = sel_tabs[:max_tabelas]

    return sel_tabs, sel_notas


def _render_tabela(st, tab, bancos=None):
    """Schema compacto: cabeçalho com o perfil e o comentário + lista 'coluna (tipo)'.

    Sem comentário por coluna (o maior gasto de tokens; as regras relevantes vêm nas
    NOTES). Tabela homônima em dois bancos rende um bloco para cada, com o rótulo
    do perfil — é o que diz ao modelo qual ``perfil`` passar. ``bancos`` restringe a
    um banco só (o endereço ``dicionario://<perfil>/<tabela>``).
    """
    blocos = []
    for banco in (bancos or st["onde"].get(tab, [])):
        cols_p, coms_p, nomes_p = st["esquemas"].get(banco, ({}, {}, {}))
        com = coms_p.get(tab, "")
        cols = ", ".join(f"{n} ({t})" for n, t, _ in cols_p.get(tab, ()))
        # Nome REAL, e não a chave em maiúsculas (banco case-sensitive).
        cab = (f"**{nomes_p.get(tab, tab)}** [perfil {_rotulo(banco)}]"
               + (f" — {com}" if com else ""))
        blocos.append(f"{cab}\ncolunas: {cols}")
    return "\n\n".join(blocos)


def recuperar(pergunta, max_tabelas=6, max_notas=4):
    """Markdown enxuto com o schema das tabelas + o corpo das NOTES relevantes."""
    return recuperar_detalhado(pergunta, max_tabelas, max_notas)[0]


def recuperar_detalhado(pergunta, max_tabelas=6, max_notas=4):
    """O texto de ``recuperar`` + o que foi selecionado: (texto, tabelas, notas, forca).

    Existe para o catálogo de perguntas (``perguntas.py``): a diferença entre
    "pergunta feita" e "pergunta que a base não sabe responder" é justamente o que
    a seleção devolveu. Sem isto o ``mcp_server`` teria de rodar a busca vetorial
    uma segunda vez só para descobrir o que a primeira já sabia.
    """
    st = _carregar()
    tabs, notas = _selecionar(pergunta, max_tabelas, max_notas)
    forca_ = forca(pergunta)
    if not tabs and not notas:
        return "", [], [], forca_
    partes = []
    if tabs:
        partes.append("### Schema das tabelas relevantes\n")
        for t in tabs:
            partes.append(_render_tabela(st, t))
            partes.append("")
    if notas:
        partes.append("### Regras/armadilhas relevantes (NOTES)\n")
        for nid in notas:
            n = st["notas"][nid]
            # O caminho vai no bloco: é como quem lê a resposta acha (e corrige)
            # a nota de origem em NOTES/<banco>/<assunto>/.
            partes.append(f"<!-- {nid} — {n['arquivo']} -->")
            partes.append(_corpo_nota(n["arquivo"]))
            partes.append("")
    return "\n".join(partes).strip(), tabs, notas, forca_


# ---------------------------------------------------------------------------
# API pública para endereçar a base por fora (as resources do mcp_server)
# ---------------------------------------------------------------------------
def listar_notas():
    """Metadados de todas as notas, na ordem do caminho (banco/assunto).

    Cada entrada traz o ``arquivo`` (NOTES/<banco>/<assunto>/nota.md), o
    ``perfil`` (banco) e o ``assunto``, que são exatamente as partes do URI
    ``nota://<banco>/<assunto>/<slug>``.
    """
    st = _carregar()
    notas = [
        {"id": nid,
         "arquivo": n.get("arquivo") or "",
         "perfil": n.get("perfil") or "",
         "assunto": n.get("assunto") or "",
         "titulo": n.get("titulo") or nid,
         "tabelas": n.get("tabelas") or []}
        for nid, n in st["notas"].items()
    ]
    notas.sort(key=lambda n: (n["arquivo"], n["id"]))
    return notas


def corpo_nota(nid):
    """Markdown de uma nota (sem frontmatter) pelo id; None se não existir."""
    n = _carregar()["notas"].get(nid)
    return _corpo_nota(n["arquivo"]) if n else None


def schema_tabela(tabela, perfil=None):
    """Bloco de schema de uma tabela; vazio se ela não está no dicionário.

    ``perfil`` pode ser o nome do perfil ou do banco de conhecimento. Sem ele,
    devolve um bloco por banco onde a tabela existe.
    """
    st = _carregar()
    tab = (tabela or "").upper()
    onde = st["onde"].get(tab)
    if not onde:
        return ""
    if perfil is None:
        return _render_tabela(st, tab)
    banco = _perfis.banco(perfil)
    if banco not in onde:
        return ""
    return _render_tabela(st, tab, [banco])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not disponivel():
        print("faltam contexto/notes.json ou edges.json — rode "
              "`python3 grafo.py construir` (e gerar_dicionario.py para o schema).",
              file=sys.stderr)
        return 1
    if not argv:
        print(__doc__)
        return 0
    if argv[0] == "--indice":
        print(indice_global())
        return 0
    pergunta = " ".join(argv)
    tabs, notas = _selecionar(pergunta)
    print(f"# pergunta: {pergunta}", file=sys.stderr)
    print(f"# tabelas: {tabs}", file=sys.stderr)
    print(f"# notas:   {notas}", file=sys.stderr)
    print(recuperar(pergunta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
