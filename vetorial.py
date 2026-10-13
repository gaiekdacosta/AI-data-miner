#!/usr/bin/env python3
"""Índice vetorial da base de conhecimento — diz ONDE está a informação.

Materializa ``contexto/vetores.json``: um vetor TF-IDF esparso por documento
(cada nota de ``NOTES/<banco>/<assunto>/`` e cada tabela dos dicionários), com o
**caminho** do arquivo ao lado. Perguntar ao índice devolve caminhos ordenados
por similaridade — é o mapa que substitui saber de cor onde cada nota mora.

TF-IDF em Python puro (sem numpy, sem banco, sem API): o arquivo é
determinístico e versionável, e regenera offline como o grafo. Cada palavra
entra no vocabulário em duas formas:

  - **radical** — plural/flexão do português normalizados
    (``devoluções``/``devolucao`` -> ``devolucao``; ``ordens`` -> ``ordem``);
  - **prefixo** de 5 letras — casa palavra com abreviação
    (``faturamento`` ~ ``fatur``, ``categoria`` ~ ``categ``).

As duas convivem no mesmo vetor e o IDF resolve o conflito sozinho: o prefixo
curto aparece em muitos documentos (peso baixo, serve de rede de segurança), o
radical inteiro é raro (peso alto, é o que decide o ranking). Por isso duas
palavras que só compartilham o começo não se confundem.

Uso:

    python3 vetorial.py construir                     # (re)gera contexto/vetores.json
    python3 vetorial.py buscar "faturamento por categoria"
    python3 vetorial.py buscar "devolução" --perfil loja --n 3
    python3 vetorial.py buscar "desconto" --tipo nota
    python3 vetorial.py mapa                          # árvore banco/assunto -> caminho
    python3 vetorial.py buscar "receita" --sem-glossario   # sem expandir sinônimo

Consumido pelo ``recuperar.py`` (etapa 1, sementes) e regenerado pelo
``gerar_dicionario.py`` junto com o grafo. O ``check.py`` falha se estiver fora
de sincronia com ``NOTES/``.
"""

import argparse
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter

try:                      # importado como pacote (mcp_server) ou script solto
    from . import grafo
except ImportError:
    import grafo

_AQUI = os.path.dirname(os.path.abspath(__file__))
_NOTES = os.path.join(_AQUI, "NOTES")
_VETORES = os.path.join(_AQUI, "contexto", "vetores.json")
_GLOSSARIO = os.path.join(_AQUI, "contexto", "glossario.txt")

# --- parâmetros do índice (mudar qualquer um exige reconstruir) ---------------
_PREFIXO = 5          # tamanho do radical curto (faturamento -> fatur)
_TOP_NOTA = 60        # termos guardados por nota
_TOP_TABELA = 40      # termos guardados por tabela
_PESO = {"titulo": 3, "id": 3, "tabelas": 2, "faceta": 2, "corpo": 1}

_STOP = {
    "de", "da", "do", "das", "dos", "no", "na", "nos", "nas", "em", "por",
    "para", "com", "sem", "que", "qual", "quais", "quanto", "quantos",
    "quantas", "quantidade", "quando", "onde", "como", "and", "the", "um",
    "uma", "uns", "umas", "ao", "aos", "os", "as", "seu", "sua", "seus",
    "suas", "meu", "minha", "me", "retorne", "retorna", "mostre", "mostra",
    "liste", "lista", "listar", "traga", "trazer", "quero", "preciso",
    "todos", "todas", "cada", "entre", "mais", "menos", "mes", "meses",
    "ano", "anos", "dia", "dias", "hoje", "ontem", "passado", "atual",
    "nao", "sim", "ser", "sao", "esta", "este", "essa", "esse", "isso",
    "pelo", "pela", "aqui", "ali", "tem", "ter", "vem", "vai", "foi",
}

_RE_TOKEN = re.compile(r"[^0-9a-z]+")
# "### TABELA (VIEW) — comentário"  /  "## Conhecimento acumulado (NOTES)"
_RE_DICT_TAB = re.compile(r"^###\s+([A-Za-z0-9_$#]+)\s+\((TABLE|VIEW)\)\s*(?:—\s*(.*))?$")


def dicionarios():
    """{banco: caminho} dos dicionários gerados (ver ``grafo.dicionarios``)."""
    return grafo.dicionarios()


# ---------------------------------------------------------------------------
# Léxico
# ---------------------------------------------------------------------------
def _norm(s):
    """Minúsculas + sem acento (devoluções -> devolucoes, ação -> acao)."""
    d = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in d if not unicodedata.combining(c)).lower()


def _desflexionar(t):
    """Plural/flexão do português -> radical (regras curtas, sem dicionário)."""
    for suf, rep in (("oes", "ao"), ("aes", "ao"), ("ais", "al"),
                     ("eis", "el"), ("ois", "ol"), ("uis", "ul"), ("ns", "m")):
        if t.endswith(suf) and len(t) > len(suf) + 1:
            return t[:-len(suf)] + rep
    if t.endswith("s") and len(t) > 3:
        return t[:-1]
    return t


def termos(texto):
    """Lista de termos (com repetição, para o TF) de um trecho de texto.

    Cada palavra rende o radical e — quando ele é mais longo que ``_PREFIXO``
    — também o prefixo curto, que é o que casa abreviação com palavra inteira.
    """
    out = []
    for bruto in _RE_TOKEN.split(_norm(texto)):
        if len(bruto) < 3 or bruto in _STOP:
            continue
        raiz = _desflexionar(bruto)
        if len(raiz) < 3:
            continue
        out.append(raiz)
        if len(raiz) > _PREFIXO:
            out.append(raiz[:_PREFIXO])
    return out


_GRUPOS = None


def _grupos_de_sinonimo():
    """[{radical, ...}, ...] lido de contexto/glossario.txt (uma linha = um grupo).

    Os termos são guardados já como radical, para casar com o vocabulário do
    índice. Arquivo ausente ou linha vazia: sem glossário, sem drama.
    """
    global _GRUPOS
    if _GRUPOS is not None:
        return _GRUPOS
    grupos = []
    if os.path.isfile(_GLOSSARIO):
        with open(_GLOSSARIO, encoding="utf-8") as fh:
            for linha in fh:
                linha = linha.split("#", 1)[0].strip()
                if not linha:
                    continue
                grupo = {t for palavra in linha.split(",") for t in termos(palavra)}
                if len(grupo) > 1:
                    grupos.append(grupo)
    _GRUPOS = grupos
    return _GRUPOS


def _expandir(contagem):
    """Acrescenta os sinônimos dos termos da pergunta. Devolve os que entraram.

    Peso 1 (uma ocorrência): a palavra escrita pela pessoa, se repetida, continua
    pesando mais que a inferida. Termo já presente não é tocado.
    """
    extras = set()
    for grupo in _grupos_de_sinonimo():
        if grupo & set(contagem):
            for t in grupo - set(contagem):
                contagem[t] = 1
                extras.add(t)
    return extras


def _tf(contagem):
    """TF sublinear: 1 + ln(n). Nota longa não atropela nota curta."""
    return {t: 1.0 + math.log(n) for t, n in contagem.items()}


def _normalizar(vetor):
    norma = math.sqrt(sum(v * v for v in vetor.values()))
    return {t: v / norma for t, v in vetor.items()} if norma else {}


# ---------------------------------------------------------------------------
# Coleta dos documentos
# ---------------------------------------------------------------------------
def _tabelas_dos_dicionarios():
    """{(banco, TABELA): (comentário, texto-do-bloco)} lido dos dicionários."""
    fora = {}
    for banco, caminho in dicionarios().items():
        atual, comentario, buffer = None, "", []

        def _fechar():
            if atual:
                fora[(banco, atual)] = (comentario, "\n".join(buffer))

        with open(caminho, encoding="utf-8") as fh:
            for linha in fh:
                if linha.startswith("## Conhecimento"):
                    break
                m = _RE_DICT_TAB.match(linha.rstrip())
                if m:
                    _fechar()
                    atual, comentario, buffer = m.group(1).upper(), (m.group(3) or "").strip(), []
                    continue
                if atual:
                    buffer.append(linha.rstrip())
        _fechar()
    return fora


# ---------------------------------------------------------------------------
# Construção
# ---------------------------------------------------------------------------
def _doc_nota(n):
    fm = n["frontmatter"]
    titulo = fm.get("titulo") or fm.get("description") or n["id"]
    tabelas = [t.upper() for t in (fm.get("tabelas") or []) if isinstance(t, str)]
    campos = [
        (titulo, _PESO["titulo"]),
        (n["id"].replace("-", " "), _PESO["id"]),
        (" ".join(tabelas), _PESO["tabelas"]),
        (f"{n['banco']} {n['assunto']}", _PESO["faceta"]),
        (n["corpo"], _PESO["corpo"]),
    ]
    contagem = Counter()
    for texto, peso in campos:
        for t in termos(texto):
            contagem[t] += peso
    return {
        "id": n["id"],
        "tipo": "nota",
        "caminho": n["arquivo"],
        "perfil": n["banco"],
        "assunto": n["assunto"],
        "titulo": titulo[:200],
    }, contagem


def _doc_tabela(banco, tab, comentario, bloco):
    colunas = " ".join(re.findall(r"^\|\s*([A-Za-z0-9_$#]+)\s*\|", bloco, re.MULTILINE))
    contagem = Counter()
    for texto, peso in ((tab.replace("_", " "), _PESO["titulo"]),
                        (comentario, _PESO["tabelas"]),
                        (banco, _PESO["faceta"]),
                        (colunas, _PESO["corpo"])):
        for t in termos(texto):
            contagem[t] += peso
    return {
        "id": f"{banco}:{tab}",
        "tipo": "tabela",
        "caminho": f"contexto/dicionario_{banco}.md#{tab}",
        "perfil": banco,
        "assunto": "schema",
        "titulo": (f"{tab} — {comentario}" if comentario else tab)[:200],
    }, contagem


def _serializar(dados):
    """JSON determinístico com UMA LINHA POR DOCUMENTO.

    ``json.dump(indent=...)`` explodiria cada par [termo, peso] em duas linhas
    (arquivo enorme e diff ilegível). Uma linha por item deixa o diff no grão
    certo: mexer numa nota muda a linha dela. ``vocabulario``/``idf`` são
    estatística global — mudam juntos a cada reconstrução, então vão compactos
    em uma linha só.
    """
    def j(v):
        return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    linhas = ["{"]
    for chave in sorted(k for k in dados if k != "itens"):
        linhas.append(f" {j(chave)}: {j(dados[chave])},")
    linhas.append(' "itens": [')
    corpo = [f"  {j(it)}" for it in dados["itens"]]
    linhas.append(",\n".join(corpo))
    linhas.append(" ]")
    linhas.append("}")
    return "\n".join(linhas) + "\n"


def construir(verbose=True):
    """Lê NOTES/ + dicionários, calcula o TF-IDF e grava contexto/vetores.json."""
    if not os.path.isdir(_NOTES):
        raise SystemExit(f"pasta NOTES não encontrada: {_NOTES}")

    brutos = []      # (meta, Counter de termos)
    for n in grafo.ler_notas():
        brutos.append(_doc_nota(n))
    for (banco, tab), (com, bloco) in sorted(_tabelas_dos_dicionarios().items()):
        brutos.append(_doc_tabela(banco, tab, com, bloco))

    # IDF sobre o corpus inteiro (notas + tabelas).
    df = Counter()
    for _, contagem in brutos:
        df.update(contagem.keys())
    total = len(brutos) or 1
    idf = {t: math.log((total + 1) / (n + 1)) + 1.0 for t, n in df.items()}

    # Só entra no vocabulário o termo que sobreviveu à poda de algum documento.
    itens, usados = [], set()
    for meta, contagem in brutos:
        pesos = {t: v * idf[t] for t, v in _tf(contagem).items()}
        limite = _TOP_NOTA if meta["tipo"] == "nota" else _TOP_TABELA
        podado = dict(sorted(pesos.items(), key=lambda kv: (-kv[1], kv[0]))[:limite])
        meta["_vetor"] = _normalizar(podado)
        usados.update(meta["_vetor"])
        itens.append(meta)

    vocabulario = sorted(usados)
    indice = {t: i for i, t in enumerate(vocabulario)}
    for meta in itens:
        vetor = meta.pop("_vetor")
        pares = sorted(((indice[t], round(p, 5)) for t, p in vetor.items()),
                       key=lambda par: (-par[1], par[0]))
        meta["vetor"] = [[i, p] for i, p in pares]
        # amostra legível: dá para entender/grepar o índice sem rodar nada
        meta["termos_topo"] = [vocabulario[i] for i, _ in pares[:8]]

    dados = {
        "versao": 1,
        "gerado_por": "vetorial.py construir",
        "config": {"prefixo": _PREFIXO, "top_nota": _TOP_NOTA,
                   "top_tabela": _TOP_TABELA, "pesos": _PESO},
        "vocabulario": vocabulario,
        "idf": [round(idf[t], 5) for t in vocabulario],
        "itens": itens,
    }
    os.makedirs(os.path.dirname(_VETORES), exist_ok=True)
    with open(_VETORES, "w", encoding="utf-8") as fh:
        fh.write(_serializar(dados))
    global _CACHE
    _CACHE = None      # quem já buscou neste processo passa a ver o índice novo

    if verbose:
        n_notas = sum(1 for i in itens if i["tipo"] == "nota")
        print(f"OK — {n_notas} notas + {len(itens) - n_notas} tabelas, "
              f"{len(vocabulario)} termos")
        print(f"  -> {_VETORES} ({os.path.getsize(_VETORES) / 1024:.0f} KB)")
    return _VETORES


# ---------------------------------------------------------------------------
# Busca
# ---------------------------------------------------------------------------
_CACHE = None


def disponivel():
    return os.path.isfile(_VETORES)


def _carregar():
    global _CACHE
    if _CACHE is None:
        with open(_VETORES, encoding="utf-8") as fh:
            d = json.load(fh)
        d["_idf"] = {t: d["idf"][i] for i, t in enumerate(d["vocabulario"])}
        d["_indice"] = {t: i for i, t in enumerate(d["vocabulario"])}
        _CACHE = d
    return _CACHE


def buscar(pergunta, n=8, tipo=None, perfil=None, assunto=None, minimo=0.02,
           glossario=True, ao_expandir=None):
    """Documentos mais parecidos com a pergunta (cosseno), do maior para o menor.

    Devolve ``[{"score", "id", "tipo", "caminho", "perfil", "assunto",
    "titulo"}, ...]`` — o ``caminho`` é o ponto: onde ler a informação. Aqui
    ``perfil`` é o BANCO de conhecimento (a pasta de NOTES).
    ``glossario=False`` desliga a expansão por sinônimo; ``ao_expandir`` recebe o
    conjunto de termos acrescentados (para a CLI mostrar o que foi inferido).
    """
    d = _carregar()
    contagem = Counter(termos(pergunta))
    if glossario:
        extras = _expandir(contagem)
        if extras and ao_expandir:
            ao_expandir(extras)
    # Só depois de expandir é que se descarta o que não está no vocabulário: o
    # sinônimo pode ser justamente o termo que existe no corpus.
    contagem = Counter({t: v for t, v in contagem.items() if t in d["_indice"]})
    if not contagem:
        return []
    q = _normalizar({t: v * d["_idf"][t] for t, v in _tf(contagem).items()})
    alvo = {d["_indice"][t]: p for t, p in q.items()}

    saida = []
    for it in d["itens"]:
        if tipo and it["tipo"] != tipo:
            continue
        if perfil and it["perfil"] != perfil:
            continue
        if assunto and it["assunto"] != assunto:
            continue
        score = sum(p * alvo[i] for i, p in it["vetor"] if i in alvo)
        if score >= minimo:
            saida.append(dict(it, score=round(score, 4), vetor=None))
    saida.sort(key=lambda x: (-x["score"], x["caminho"]))
    for s in saida:
        s.pop("vetor", None)
        s.pop("termos_topo", None)
    return saida[:n]


def caminho_de(nid):
    """Caminho do arquivo de uma nota/tabela pelo id (None se não existir)."""
    for it in _carregar()["itens"]:
        if it["id"] == nid:
            return it["caminho"]
    return None


def mapa(perfil=None):
    """Árvore banco/assunto -> caminho + título, para ler o índice de relance."""
    d = _carregar()
    linhas, atual = [], None
    notas = [i for i in d["itens"] if i["tipo"] == "nota"
             and (not perfil or i["perfil"] == perfil)]
    for it in sorted(notas, key=lambda x: x["caminho"]):
        grupo = f"{it['perfil']}/{it['assunto']}"
        if grupo != atual:
            atual = grupo
            linhas.append(f"\n{grupo}/")
        linhas.append(f"  {it['caminho']}\n      {it['titulo'][:120]}")
    return "\n".join(linhas).strip()


# ---------------------------------------------------------------------------
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(description="Índice vetorial da base de conhecimento.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("construir", help="regera contexto/vetores.json")
    b = sub.add_parser("buscar", help="caminhos mais relevantes para uma pergunta")
    b.add_argument("pergunta", nargs="+")
    b.add_argument("--n", type=int, default=8)
    b.add_argument("--tipo", choices=("nota", "tabela"))
    b.add_argument("--perfil", help="banco de conhecimento (pasta de NOTES)")
    b.add_argument("--assunto")
    b.add_argument("--sem-glossario", action="store_true",
                   help="não expande a pergunta com sinônimos")
    m = sub.add_parser("mapa", help="árvore banco/assunto das NOTES")
    m.add_argument("--perfil")
    args = p.parse_args(argv)

    if args.cmd == "construir":
        construir()
        return 0
    if not disponivel():
        print(f"falta {_VETORES} — rode: python3 vetorial.py construir", file=sys.stderr)
        return 1
    if args.cmd == "buscar":
        achados = buscar(
            " ".join(args.pergunta), n=args.n, tipo=args.tipo, perfil=args.perfil,
            assunto=args.assunto, glossario=not args.sem_glossario,
            ao_expandir=lambda ts: print(f"# glossário acrescentou: "
                                         f"{', '.join(sorted(ts))}", file=sys.stderr),
        )
        if not achados:
            print("(nada acima do corte — tente outras palavras)", file=sys.stderr)
            return 0
        for a in achados:
            print(f"{a['score']:.3f}  {a['caminho']}")
            print(f"         [{a['tipo']}/{a['perfil']}] {a['titulo'][:110]}")
        return 0
    if args.cmd == "mapa":
        print(mapa(args.perfil))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
