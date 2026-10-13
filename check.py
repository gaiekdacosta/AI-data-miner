#!/usr/bin/env python3
"""Verificador da base de conhecimento — rode antes de commitar (e no CI).

Python puro, sem banco e sem dependências. Checa:

1. NOTES/<banco>/<assunto>/*.md — nota no lugar certo (banco conhecido, assunto
   obrigatório) e com o frontmatter concordando com a pasta; ``pontes:`` no
   formato ``A -> B (colunas)``; wikilinks ``[[slug]]`` apontando para nota que
   existe (aviso, não erro).
2. Índices em sincronia — regenera notes.json/edges.json (grafo.py) e
   vetores.json (vetorial.py) e compara com o que está no disco. Se divergir, os
   arquivos JÁ FICAM regenerados: revise o diff e inclua no commit (no CI isso
   falha o job, que é a intenção).
3. Vazamentos óbvios em NOTES/ e nos cabeçalhos manuais do contexto/ — CPF
   (exceto placeholders) e credencial em texto (``senha/password = ...``,
   ``IDENTIFIED BY``).

"Banco conhecido" é: um dicionário gerado (``contexto/dicionario_<banco>.md``)
ou um perfil do .env. No CI não há .env, então valem os dicionários versionados.

E imprime o **relatório de cobertura**: as lacunas da base (tabela citada sem
schema, nota que não ensina aresta ao grafo). Não são erros — é a fila de
trabalho da próxima sessão, então não mexem no código de saída.

Uso:

    python3 check.py              # sai com código 1 se houver erro
    python3 check.py --cobertura  # o relatório de cobertura sem truncar
"""

import glob
import os
import re
import sys

import grafo
import perfis as _perfis
import vetorial

_AQUI = os.path.dirname(os.path.abspath(__file__))

_RE_CPF = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
_PLACEHOLDERS_CPF = {"123.456.789-01", "000.000.000-00", "111.111.111-11"}
_RE_CREDENCIAL = re.compile(
    r"\b(?:senha|password|pwd)\s*[:=]\s*['\"]?[^\s'\"*]{3,}"
    r"|\bIDENTIFIED\s+BY\b",
    re.IGNORECASE,
)

erros, avisos = [], []


def _bancos_conhecidos():
    conhecidos = set(grafo.dicionarios())
    for p in _perfis.PERFIS:
        conhecidos.add(p)
        conhecidos.add(_perfis.banco(p))
    return conhecidos


def _notas():
    """(caminho relativo, texto) de cada nota — varredura recursiva de NOTES/."""
    for n in grafo.ler_notas():
        yield n["arquivo"], n["texto"]


def checar_frontmatter():
    conhecidos = _bancos_conhecidos()
    slugs = set()
    pendentes = []  # (nota, links) para validar depois de conhecer todos os slugs
    for rel, texto in _notas():
        partes = rel.split("/")[1:]          # tira o "NOTES/"
        if len(partes) < 3:
            erros.append(f"{rel}: nota fora de NOTES/<banco>/<assunto>/ "
                         "(um nível de banco e um de assunto são obrigatórios)")
        elif partes[0] not in conhecidos:
            erros.append(f"{rel}: banco '{partes[0]}' desconhecido — gere o "
                         f"dicionário dele ou declare o perfil no .env "
                         f"(conhecidos: {', '.join(sorted(conhecidos)) or 'nenhum'})")
        if not texto.startswith("---"):
            erros.append(f"{rel}: sem frontmatter (deve começar com ---)")
            continue
        fm, corpo = grafo._parse_frontmatter(texto)
        if not fm:
            erros.append(f"{rel}: frontmatter vazio ou não fechado com ---")
            continue
        slugs.add(fm.get("name") or os.path.basename(rel)[:-3])
        # A pasta é a organização visível; o frontmatter é o que os scripts leem.
        # Divergir entre os dois entrega a nota ao banco errado sem ninguém ver.
        if len(partes) >= 3 and fm.get("perfil") != partes[0]:
            erros.append(f"{rel}: perfil do frontmatter ({fm.get('perfil')!r}) "
                         f"não confere com a pasta ({partes[0]!r})")
        for p in fm.get("pontes", []) or []:
            if not isinstance(p, str) or not grafo._RE_PONTE.search(p):
                erros.append(f"{rel}: ponte malformada (esperado 'A -> B (...)'): {p!r}")
        links = grafo._RE_LINK.findall(corpo)
        if links:
            pendentes.append((rel, links))
    for rel, links in pendentes:
        for l in links:
            if l.strip() not in slugs:
                avisos.append(f"{rel}: wikilink para nota inexistente: [[{l}]]")


def checar_indices_sincronizados():
    caminhos = (grafo._NOTES_JSON, grafo._EDGES_JSON, vetorial._VETORES)

    def _ler():
        conteudo = {}
        for c in caminhos:
            if os.path.isfile(c):
                with open(c, encoding="utf-8") as fh:
                    conteudo[c] = fh.read()
            else:
                conteudo[c] = None
        return conteudo

    antes = _ler()
    grafo.construir(verbose=False)
    vetorial.construir(verbose=False)
    depois = _ler()
    for c in caminhos:
        if antes[c] != depois[c]:
            erros.append(
                f"{os.path.relpath(c, _AQUI)}: estava fora de sincronia com NOTES/ "
                "(regenerado agora — revise o diff e inclua no commit)"
            )


def checar_vazamentos():
    """CPF e credencial em texto, nas notas e nos cabeçalhos escritos à mão.

    São os dois lugares onde gente cola exemplo de SQL e resultado de consulta —
    e é por eles que dado de produção entraria num repositório.
    """
    alvos = list(_notas())
    for caminho in sorted(glob.glob(os.path.join(_AQUI, "contexto", "cabecalho_*.md"))):
        with open(caminho, encoding="utf-8") as fh:
            alvos.append((os.path.relpath(caminho, _AQUI), fh.read()))
    for rel, texto in alvos:
        for m in _RE_CPF.finditer(texto):
            if m.group(0) not in _PLACEHOLDERS_CPF:
                erros.append(f"{rel}: possível CPF real no texto: {m.group(0)}")
        for m in _RE_CREDENCIAL.finditer(texto):
            erros.append(f"{rel}: possível credencial em texto: {m.group(0)!r}")


def relatorio_cobertura(limite=12):
    """Lacunas de conhecimento da base — a fila de trabalho, não defeitos.

    Três perguntas:

    1. que tabela as NOTES citam sem que exista schema no dicionário DAQUELE
       banco — é o pior caso silencioso: o modelo recebe a regra de negócio da
       nota, não recebe coluna nenhuma e inventa os nomes;
    2. que nota não declara ``pontes:`` — entra no vetorial e não ensina aresta
       nenhuma ao grafo, então nunca aparece num ``grafo.py caminho``;
    3. que nota não declara ``tabelas:`` — o grafo não a liga a nada.
    """
    catalogo = vetorial._tabelas_dos_dicionarios()
    if not catalogo:
        return ["(nenhum dicionário gerado — cobertura não avaliada)"]

    # Conhecido POR BANCO, não no geral: a mesma sigla pode existir em dois bancos
    # com colunas diferentes, então citar a tabela do outro banco é lacuna.
    conhecidas = {}
    for banco, tab in catalogo:
        conhecidas.setdefault(banco, set()).add(tab)

    sem_schema, sem_ponte, sem_tabela = {}, [], []
    for n in grafo.ler_notas():
        fm = n["frontmatter"]
        rel = n["arquivo"][len("NOTES/"):]
        banco = _perfis.banco(n["banco"])
        tabs = [t.upper() for t in (fm.get("tabelas") or []) if isinstance(t, str)]
        if not tabs:
            sem_tabela.append(rel)
        if not (fm.get("pontes") or []):
            sem_ponte.append(rel)
        for t in tabs:
            if t not in conhecidas.get(banco, set()):
                sem_schema.setdefault((banco, t), []).append(rel)

    def _bloco(titulo, itens, formata):
        if not itens:
            return [f"  0 {titulo}"]
        linhas = [f"  {len(itens)} {titulo}:"]
        mostrados = itens if limite is None else itens[:limite]
        linhas += [f"      {formata(i)}" for i in mostrados]
        if limite is not None and len(itens) > limite:
            linhas.append(f"      (+{len(itens) - limite} — veja com --cobertura)")
        return linhas

    out = ["COBERTURA — lacunas conhecidas (não são erros; é a fila de trabalho)"]
    out += _bloco(
        "tabela(s) citada(s) sem schema no dicionário do próprio banco",
        sorted(sem_schema),
        lambda k: f"{k[1]} [{k[0]}]  <- {', '.join(sorted(sem_schema[k]))}",
    )
    out += _bloco("nota(s) sem `pontes:` (não ensinam aresta ao grafo)",
                  sorted(sem_ponte), lambda r: r)
    out += _bloco("nota(s) sem `tabelas:` no frontmatter",
                  sorted(sem_tabela), lambda r: r)
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    checar_frontmatter()
    checar_indices_sincronizados()
    checar_vazamentos()

    for a in avisos:
        print(f"AVISO  {a}")
    for e in erros:
        print(f"ERRO   {e}")
    if erros:
        print(f"\n{len(erros)} erro(s), {len(avisos)} aviso(s).")
        return 1
    print(f"OK — base de conhecimento íntegra ({len(avisos)} aviso(s)).")

    # Cobertura é informativa por escolha: reprovar o commit por débito antigo
    # só ensinaria a ignorar o check.
    print()
    print("\n".join(relatorio_cobertura(limite=None if "--cobertura" in argv else 12)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
