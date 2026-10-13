#!/usr/bin/env python3
"""Catálogo das perguntas — o que as pessoas querem saber, e o que a base ainda
não sabe responder.

**Não é auditoria.** A `auditoria.py` responde "quem consultou o quê", guarda o
SQL executado (inclusive o recusado) e existe para prestação de contas. Este
arquivo responde outra pergunta, de melhoria: *quais perguntas chegam, com que
frequência, e o que o `recuperar_contexto` conseguiu entregar para cada uma.*

O produto é uma **fila de trabalho para a base de conhecimento**: pergunta que
volta e não recupera nada é NOTE a escrever, e é isso que faz a resposta seguinte
ser melhor. E vai pela pergunta, não minerando SQL: o SQL diz qual tabela
alguém leu, a pergunta diz o que a pessoa queria e a base não deu.

Um arquivo por dia em `perguntas/perguntas-AAAA-MM-DD.jsonl`, append-only, com
duas espécies de linha:

    {"ts":"2026-01-15T09:12:03", "tipo":"pergunta", "pessoa":"maria_silva",
     "origem":"recuperar_contexto", "pergunta":"faturamento por categoria em julho",
     "tabelas":["PEDIDOS"], "notas":["faturamento-so-pedido-faturado"], "achou":true,
     "sessao":"a1b2c3d4"}

    {"ts":"2026-01-15T09:12:20", "tipo":"sql", "pessoa":"maria_silva",
     "pergunta":"faturamento por categoria em julho", "perfil":"vendas",
     "sql":"SELECT ...", "linhas":37, "ms":412, "sessao":"a1b2c3d4"}

A linha `sql` só é escrita quando a consulta **deu certo** e existe pergunta
conhecida na sessão para amarrar. De propósito: aqui a query só interessa como
*resposta que funcionou* — é o rascunho da nota futura. Recusa, erro de banco e
tentativa barrada continuam onde devem, na trilha de auditoria.

Decisões herdadas da trilha, pelos mesmos motivos: **nunca derruba a consulta**
(falha ao gravar é engolida, com um aviso único no stderr), **um arquivo por dia**
(expurgo é `rm` dos dias antigos) e **0600, fora do git** — pergunta de negócio
carrega nome de pessoa, de projeto e de cliente com toda a naturalidade.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta

try:  # como pacote
    from . import vetorial
except ImportError:  # como script direto
    import vetorial

DIR = os.environ.get("PERGUNTAS_DIR",
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "perguntas"))
MAX_PERGUNTA = int(os.environ.get("PERGUNTAS_MAX_TEXTO", "1000"))
MAX_SQL = int(os.environ.get("PERGUNTAS_MAX_SQL", "4000"))
# Semelhança mínima (Jaccard dos radicais) para duas perguntas contarem como a
# mesma. 0.6 junta "quantos pedidos FORAM cancelados em julho" com "pedidos
# cancelados de julho" (3 de 4 radicais) e mantém separadas "...em julho" e
# "...em agosto" (2 de 4) — o mês muda a pergunta.
LIMIAR = float(os.environ.get("PERGUNTAS_LIMIAR", "0.6"))
# Abaixo desta força (score da melhor nota, ver `recuperar.forca`) a base
# respondeu com o que tinha, não com o que servia. 0,15 é ponto de partida:
# calibre com as SUAS perguntas (a mediana das forças do catálogo é uma boa
# régua) para a fila marcar uma fração pequena delas — fila de trabalho semanal,
# não enxurrada.
FORCA_MINIMA = float(os.environ.get("PERGUNTAS_FORCA_MINIMA", "0.15"))

_TRAVA = threading.Lock()
_avisou = False


# ---------------------------------------------------------------------------
# Agrupamento: "quantas vezes ESTA pergunta já foi feita"
#
# Duas pessoas nunca digitam a mesma frase, e a mesma pessoa reformula. Agrupar
# por texto literal daria uma lista de linhas únicas — inútil para achar
# recorrência.
#
# A normalização vem do índice vetorial (sem acento, sem stopword,
# plural/flexão -> radical) porque é a mesma língua e o mesmo vocabulário do
# resto do projeto. NÃO usa `vetorial.termos()`: ela emite também o prefixo de 5
# letras (fatur ~ faturamento), que serve para o TF-IDF casar abreviação e aqui
# só encheria o conjunto de termos redundantes, distorcendo a semelhança.
# ---------------------------------------------------------------------------
def radicais(pergunta: str) -> set:
    """Conjunto de radicais de uma pergunta — a base da comparação."""
    saida = set()
    for bruto in vetorial._RE_TOKEN.split(vetorial._norm(pergunta or "")):
        if len(bruto) < 3 or bruto in vetorial._STOP:
            continue
        raiz = vetorial._desflexionar(bruto)
        if len(raiz) >= 3:
            saida.add(raiz)
    return saida


def semelhanca(a: set, b: set) -> float:
    """Jaccard: quanto os dois conjuntos de radicais se sobrepõem (0 a 1)."""
    if not a or not b:
        return 1.0 if a == b else 0.0
    return len(a & b) / len(a | b)


def chave(pergunta: str) -> str:
    """Assinatura estável de uma pergunta (radicais ordenados).

    Só identifica; quem decide se duas perguntas são a mesma é a `semelhanca`.
    Pergunta feita só de stopword/número não tem radical: cai no texto
    normalizado, que ao menos agrupa a repetição literal.
    """
    r = radicais(pergunta)
    return " ".join(sorted(r)) if r else vetorial._norm(pergunta or "").strip()[:80]


def _arquivo(quando: datetime) -> str:
    return os.path.join(DIR, f"perguntas-{quando:%Y-%m-%d}.jsonl")


def _gravar(registro: dict) -> None:
    global _avisou
    try:
        with _TRAVA:
            os.makedirs(DIR, mode=0o750, exist_ok=True)
            caminho = _arquivo(datetime.now())
            novo = not os.path.exists(caminho)
            with open(caminho, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
            if novo:  # pergunta de negócio tem nome de gente dentro
                os.chmod(caminho, 0o600)
    except Exception as e:  # catálogo nunca derruba consulta
        if not _avisou:
            import sys
            print(f"[perguntas] não consegui gravar em {DIR}: {e}", file=sys.stderr)
            _avisou = True


def registrar(pessoa, pergunta, origem, tabelas=None, notas=None, sessao=None,
              forca=None) -> None:
    """Uma pergunta que chegou ao servidor.

    `origem` distingue de onde veio o texto, e a diferença importa na leitura:
    `prompt` é o que a PESSOA digitou (roteiro `pergunta`), `recuperar_contexto`
    é o que o MODELO pediu à base — normalmente uma reformulação, às vezes várias
    por pergunta original.

    `achou` só existe para `recuperar_contexto`: ausência de contexto é lacuna da
    base, e o prompt não recupera nada para poder ter lacuna.

    `forca` (0..1) é o score da melhor nota — ver `recuperar.forca`. Existe
    porque `achou` sozinho é inútil: o cosseno devolve alguma nota para qualquer
    texto, então `achou` é `True` em 100% dos registros e a fila de lacunas nunca
    dispara. Com a força, "a base respondeu mal" vira ordenável.
    """
    pergunta = (pergunta or "").strip()
    if not pergunta:
        return
    registro = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tipo": "pergunta",
        "pessoa": pessoa or "?",
        "origem": origem,
        "pergunta": pergunta[:MAX_PERGUNTA],
    }
    if origem == "recuperar_contexto":
        registro["tabelas"] = list(tabelas or [])[:12]
        registro["notas"] = list(notas or [])[:8]
        registro["achou"] = bool(tabelas or notas)
        if forca is not None:
            registro["forca"] = round(float(forca), 4)
    if sessao:
        registro["sessao"] = sessao
    _gravar(registro)


def registrar_sql(pessoa, pergunta, perfil, sql, linhas=None, ms=None, sessao=None) -> None:
    """A query que respondeu a uma pergunta (só sucesso — ver docstring do módulo)."""
    pergunta = (pergunta or "").strip()
    if not pergunta or not sql:
        return
    registro = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tipo": "sql",
        "pessoa": pessoa or "?",
        "pergunta": pergunta[:MAX_PERGUNTA],
        "perfil": perfil,
        "sql": sql[:MAX_SQL],
    }
    if linhas is not None:
        registro["linhas"] = linhas
    if ms is not None:
        registro["ms"] = int(ms)
    if sessao:
        registro["sessao"] = sessao
    _gravar(registro)


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------
def ler(dias=30, limite=20000, pessoa=None):
    """Registros do mais recente para o mais antigo.

    Janela padrão de 30 dias (a da trilha é 7): recorrência de pergunta só
    aparece no mês — no dia, tudo parece pergunta nova.
    """
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


_CACHE_FORCA = {}


def _forca_agora(pergunta):
    """Força da pergunta contra a base ATUAL — só para o modo `--recalcular`.

    Import tardio de propósito: o `recuperar` carrega notes/edges/dicionários, e
    o `perguntas` é importado pelo `mcp_server` no caminho de toda consulta.
    """
    if pergunta in _CACHE_FORCA:
        return _CACHE_FORCA[pergunta]
    try:
        try:
            from . import recuperar
        except ImportError:
            import recuperar
        v = recuperar.forca(pergunta)
    except Exception:
        v = 0.0
    _CACHE_FORCA[pergunta] = v
    return v


def catalogo(dias=30, pessoa=None, max_sqls=3, recalcular=False):
    """Perguntas agrupadas por semelhança, da mais pedida para a menos.

    Cada entrada tem o que se precisa para decidir escrever uma nota: quantas
    vezes foi perguntada, por quem, o que a base entregou (notas e tabelas) e as
    queries que funcionaram — o rascunho pronto do exemplo SQL da nota.

    O agrupamento é guloso e de uma passada: como `ler` vem do mais recente para
    o mais antigo, a **redação mais nova** ancora o grupo e as anteriores se
    juntam a ela. Custo é O(grupos) por registro; com centenas de grupos no mês
    isso é irrelevante, e evita o passo de clusterização que ninguém entenderia
    depois.
    """
    grupos = []
    for reg in ler(dias=dias, pessoa=pessoa):
        texto = reg.get("pergunta")
        if not texto:
            continue
        rad = radicais(texto)
        alvo = None
        for g in grupos:
            if semelhanca(rad, g["_rad"]) >= LIMIAR:
                alvo = g
                break
        if alvo is None:
            alvo = {
                "chave": chave(texto), "pergunta": texto, "_rad": rad,
                "vezes": 0, "respostas": 0, "pessoas": [],
                "primeiro": reg["ts"], "ultimo": reg["ts"],
                "notas": {}, "tabelas": {}, "sem_contexto": 0, "forcas": [],
                "sqls": [], "_variantes": set(),
            }
            grupos.append(alvo)

        alvo["primeiro"] = min(alvo["primeiro"], reg["ts"])
        alvo["ultimo"] = max(alvo["ultimo"], reg["ts"])
        quem = reg.get("pessoa", "?")
        if quem not in alvo["pessoas"]:
            alvo["pessoas"].append(quem)

        if reg.get("tipo") == "sql":
            alvo["respostas"] += 1
            if len(alvo["sqls"]) < max_sqls:
                alvo["sqls"].append({c: reg.get(c) for c in
                                     ("ts", "pessoa", "perfil", "sql", "linhas", "ms")})
            continue

        alvo["vezes"] += 1
        alvo["_variantes"].add(texto)
        for nid in reg.get("notas") or ():
            alvo["notas"][nid] = alvo["notas"].get(nid, 0) + 1
        for tab in reg.get("tabelas") or ():
            alvo["tabelas"][tab] = alvo["tabelas"].get(tab, 0) + 1
        if reg.get("origem") == "recuperar_contexto" and not reg.get("achou"):
            alvo["sem_contexto"] += 1
        if reg.get("forca") is not None:
            alvo["forcas"].append(float(reg["forca"]))
        elif recalcular and reg.get("origem") == "recuperar_contexto":
            # Registro sem força gravada (de uma versão anterior do catálogo).
            # Recalcular pontua contra a base de HOJE — que é o que interessa para decidir o
            # que escrever agora, mas NÃO é o que o usuário recebeu na época.
            alvo["forcas"].append(_forca_agora(texto))

    for g in grupos:
        g["variantes"] = len(g.pop("_variantes"))
        g.pop("_rad")
        # A MELHOR tentativa do grupo, não a média: se alguma reformulação achou
        # a nota certa, a base cobre o assunto e o que faltou foi vocabulário —
        # caso de glossário, não de nota nova.
        fs = g.pop("forcas")
        g["forca"] = round(max(fs), 4) if fs else None
        g["notas"] = sorted(g["notas"].items(), key=lambda kv: -kv[1])
        g["tabelas"] = sorted(g["tabelas"].items(), key=lambda kv: -kv[1])
    # Dois passes: dentro do empate de frequência, o mais recente primeiro (sort
    # é estável, e não existe "-ts" para caber numa key só).
    grupos.sort(key=lambda g: g["ultimo"], reverse=True)
    grupos.sort(key=lambda g: -g["vezes"])
    return grupos


def lacunas(dias=30, grupos=None):
    """O que a base não responde bem — a fila de notas a escrever.

    Três motivos, e a diferença decide o trabalho:

    * `nada_recuperado` — a busca voltou vazia. Ou o assunto não existe na base,
      ou o vocabulário da pergunta não casa com o das notas (sinônimo para o
      `contexto/glossario.txt`). Vale mesmo com uma ocorrência só.
    * `contexto_fraco` — recuperou, mas a melhor nota ficou abaixo de
      `FORCA_MINIMA`: o modelo levou para o prompt a nota mais próxima que
      existia, que não é a nota certa. É o motivo que mais dispara, e o único que
      enxerga o caso ruim de verdade — sem ele a fila viveria vazia, porque
      `achou` é `True` em quase todo registro (o cosseno sempre devolve algo).
    * `sem_nota` — achou tabela, mas nenhuma NOTE. O modelo recebeu schema sem
      regra: é onde ele acerta o nome da coluna e erra o critério. Só conta a
      partir da 2ª vez, senão toda pergunta exploratória entraria na fila.

    `grupos` aceita um catálogo já montado, para a tela não reler os arquivos.
    """
    fila = []
    for g in (catalogo(dias=dias) if grupos is None else grupos):
        if g["sem_contexto"]:
            motivo = "nada_recuperado"
        elif g["forca"] is not None and g["forca"] < FORCA_MINIMA:
            motivo = "contexto_fraco"
        elif not g["notas"] and g["vezes"] >= 2:
            motivo = "sem_nota"
        else:
            continue
        fila.append(dict(g, motivo=motivo))
    # Ordem de trabalho: primeiro o que não recuperou nada, depois o contexto
    # fraco (do mais fraco para o menos), e o resto por frequência. Dentro de
    # cada motivo, quem foi mais perguntado vem antes.
    ordem = {"nada_recuperado": 0, "contexto_fraco": 1, "sem_nota": 2}
    fila.sort(key=lambda g: (ordem[g["motivo"]],
                             g["forca"] if g["motivo"] == "contexto_fraco" else 0,
                             -g["vezes"]))
    return fila


# ---------------------------------------------------------------------------
# Dossiê: a lacuna virada em rascunho de nota
#
# Escrever a nota é trabalho de julgamento (só quem lê o banco decide o que é
# regra e o que é coincidência), mas TUDO que cerca esse julgamento é mecânico:
# quais perguntas caíram no mesmo assunto, quem perguntou, que SQL respondeu, que
# tabelas essas SQL tocaram, que notas a base já tem por perto. Juntar isso à mão
# era o caro da tarefa — e é o que este gerador entrega pronto.
#
# A saída é markdown para ser lida por gente ou colada num prompt. Não escreve em
# NOTES/: a nota entra no repo pela mão de quem verificou o dado no banco, e o
# `check.py` continua sendo o portão.
# ---------------------------------------------------------------------------

# Identificador depois de FROM/JOIN. Mesmo espírito do `gerar_dicionario.py`,
# reescrito aqui para o módulo seguir sem dependência (ele roda dentro do
# mcp_server, no caminho de toda consulta).
_RE_FROM_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_$.]*)", re.I)
_RE_COMENTARIO = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_NAO_TABELA = {"SELECT", "DUAL", "TABLE", "LATERAL", "VALUES", "RDB$DATABASE"}


def _tabelas_do_sql(sql, aliases_cte):
    """Tabelas reais citadas por um SELECT — sem CTE, sem palavra-chave.

    Filtrar as CTE importa: `WITH base AS (...) SELECT ... FROM base` colocaria
    `BASE` na lista de tabelas do dossiê, e alguém iria procurá-la no banco.
    """
    limpo = _RE_COMENTARIO.sub(" ", sql or "")
    saida = []
    for m in _RE_FROM_JOIN.findall(limpo):
        nome = m.split(".")[-1].upper()
        if nome in _NAO_TABELA or nome in aliases_cte or len(nome) < 2:
            continue
        if nome not in saida:
            saida.append(nome)
    return saida


_RE_CTE = re.compile(r"(?:\bWITH\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", re.I)


def _ctes(sql):
    return {m.upper() for m in _RE_CTE.findall(sql or "")}


def dossie_dados(dias=30, limite=5, max_sqls=6, grupos=None, recalcular=False):
    """A mesma apuração do `dossie()`, em estrutura — para quem vai DESENHAR.

    O markdown existe para ser copiado para dentro de um prompt; a tela do painel
    quer outra coisa (triagem, ordenar por gravidade, esconder a SQL até alguém
    pedir). Renderizar markdown de volta em HTML para recuperar a estrutura que
    este módulo já tinha seria trabalho perdido e frágil, então as duas saídas
    nascem daqui.
    """
    if grupos is None:
        grupos = catalogo(dias=dias, max_sqls=max_sqls, recalcular=recalcular)
    fila = lacunas(dias=dias, grupos=grupos)[:limite]

    saida = []
    for g in fila:
        perfis, tabelas = {}, {}
        for q in g["sqls"]:
            perfis[q.get("perfil")] = perfis.get(q.get("perfil"), 0) + 1
            for t in _tabelas_do_sql(q.get("sql"), _ctes(q.get("sql"))):
                tabelas[t] = tabelas.get(t, 0) + 1
        saida.append({
            "pergunta": g["pergunta"],
            "motivo": g["motivo"],
            "forca": g["forca"],
            "vezes": g["vezes"],
            "variantes": g["variantes"],
            "pessoas": g["pessoas"],
            "primeiro": g["primeiro"],
            "ultimo": g["ultimo"],
            "respostas": g["respostas"],
            "notas_oferecidas": [n for n, _ in g["notas"][:4]],
            "perfil": max(perfis, key=perfis.get) if perfis else "?",
            "tabelas": sorted(tabelas, key=lambda t: -tabelas[t])[:8],
            "sqls": g["sqls"],
        })

    return {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "dias": dias,
        "forca_minima": FORCA_MINIMA,
        "assuntos": len(grupos),
        "perguntas": sum(g["vezes"] for g in grupos),
        "consultas_ok": sum(g["respostas"] for g in grupos),
        "lacunas": saida,
    }


def dossie(dias=30, limite=5, max_sqls=6, grupos=None, recalcular=False, dados=None):
    """Markdown com um rascunho de nota por lacuna, da mais urgente para a menos."""
    d = dados or dossie_dados(dias=dias, limite=limite, max_sqls=max_sqls,
                              grupos=grupos, recalcular=recalcular)
    if not d["lacunas"]:
        return ("# Dossiê de lacunas\n\n"
                f"Nenhuma lacuna nos últimos {d['dias']} dias "
                f"(força mínima {d['forca_minima']}). Nada a escrever.\n")

    partes = ["# Dossiê de lacunas — rascunhos de NOTE",
              "",
              f"Janela: {d['dias']} dias. Força mínima: {d['forca_minima']}. "
              f"{len(d['lacunas'])} lacuna(s) de {d['assuntos']} assunto(s) perguntados.",
              "",
              "Cada bloco abaixo é um rascunho: o frontmatter já vem preenchido com",
              "as tabelas que as consultas bem-sucedidas tocaram. **Confirme cada",
              "afirmação no banco antes de virar nota** — a SQL registrada rodou, o",
              "que não quer dizer que a leitura dela estava certa.",
              ""]

    for i, g in enumerate(d["lacunas"], 1):
        forca = "—" if g["forca"] is None else f"{g['forca']:.3f}"
        partes += [
            f"## {i}. {g['pergunta'][:110]}",
            "",
            f"* **motivo:** `{g['motivo']}` — força {forca} "
            f"(mínima {d['forca_minima']})",
            f"* **perguntada:** {g['vezes']}x em {g['variantes']} redação(ões), "
            f"por {', '.join(g['pessoas'])}",
            f"* **período:** {g['primeiro'][:16]} → {g['ultimo'][:16]}",
            f"* **respondida por:** {g['respostas']} consulta(s) que deram certo",
            "* **a base ofereceu:** "
            + (", ".join(f"`{n}`" for n in g["notas_oferecidas"]) or "_nada_"),
            "",
            "```yaml",
            "# rascunho — reveja tudo antes de salvar em NOTES/<banco>/<assunto>/",
            f"perfil: {g['perfil']}",
            f"tabelas: [{', '.join(g['tabelas'])}]",
            "pontes: []   # preencha com as arestas que a SQL abaixo revela",
            "```",
            "",
            f"> {g['pergunta']}",
            "",
        ]
        if g["sqls"]:
            partes += [f"**Consultas que responderam** (as {len(g['sqls'])} mais "
                       "recentes):", ""]
            for q in g["sqls"]:
                partes += [f"<!-- {q['ts'][:16]} · {q['pessoa']} · perfil "
                           f"{q['perfil']} · {q.get('linhas','?')} linhas · "
                           f"{q.get('ms','?')} ms -->",
                           "```sql", (q.get("sql") or "").strip(), "```", ""]
        partes.append("---")
        partes.append("")
    return "\n".join(partes)


def resumo(grupos):
    """Números do topo da tela, a partir de um catálogo já montado."""
    return {
        "registros": sum(g["vezes"] for g in grupos),
        "distintas": len([g for g in grupos if g["vezes"]]),
        "pessoas": len({p for g in grupos for p in g["pessoas"]}),
        "respondidas": sum(g["respostas"] for g in grupos),
    }


def gravar_dossie(pasta, dias=30, guardar=12, recalcular=True):
    """Grava dossie-AAAA-MM-DD.{json,md} em `pasta` — o que a tela /dossie lê.

    As duas saídas nascem da MESMA apuração (o .md é render do .json), e cada uma
    vai primeiro para um `.parcial` e só então é renomeada: um processo que morre
    no meio nunca deixa a tela com relatório truncado — que ela mostraria como
    "nenhuma lacuna", exatamente a mentira que este relatório existe para não
    contar. Mantém os `guardar` relatórios mais recentes.
    """
    os.makedirs(pasta, exist_ok=True)
    d = dossie_dados(dias=dias, recalcular=recalcular)
    base = os.path.join(pasta, f"dossie-{datetime.now():%Y-%m-%d}")
    saidas = {base + ".json": json.dumps(d, ensure_ascii=False),
              base + ".md": dossie(dados=d)}
    for destino, conteudo in saidas.items():
        parcial = destino + ".parcial"
        with open(parcial, "w", encoding="utf-8") as fh:
            fh.write(conteudo)
        os.chmod(parcial, 0o640)     # pergunta de negócio tem nome de gente dentro
        os.replace(parcial, destino)
    for ext in (".md", ".json"):
        antigos = sorted(n for n in os.listdir(pasta)
                         if n.startswith("dossie-") and n.endswith(ext))
        for nome in antigos[:-guardar] if guardar > 0 else []:
            os.remove(os.path.join(pasta, nome))
    return base + ".md", len(d["lacunas"])


if __name__ == "__main__":  # docker compose exec mcp python /app/perguntas.py 30
    import sys

    argv = sys.argv[1:]
    modo_dossie = "--dossie" in argv
    modo_json = "--json" in argv
    recalcular = "--recalcular" in argv
    gravar = None
    if "--gravar" in argv:
        i = argv.index("--gravar")
        gravar = argv[i + 1]
        del argv[i:i + 2]
    guardar = 12
    if "--guardar" in argv:
        i = argv.index("--guardar")
        guardar = int(argv[i + 1])
        del argv[i:i + 2]
    argv = [a for a in argv if not a.startswith("--")]
    dias = int(argv[0]) if argv else 30

    if gravar:
        caminho, n = gravar_dossie(gravar, dias=dias, guardar=guardar, recalcular=recalcular)
        print(f"gerado {caminho} ({n} lacuna(s))")
        raise SystemExit(0)

    if modo_dossie:
        # Sai só o markdown: a saída é para redirecionar num arquivo ou colar
        # num prompt, então nada de cabeçalho decorativo no stdout.
        d = dossie_dados(dias=dias, recalcular=recalcular)
        print(json.dumps(d, ensure_ascii=False) if modo_json
              else dossie(dados=d), end="")
        raise SystemExit(0)

    grupos = catalogo(dias=dias, recalcular=recalcular)
    print(f"resumo({dias}d): {resumo(grupos)}\n")
    print("== mais perguntadas ==")
    for g in grupos[:15]:
        forca = "—" if g["forca"] is None else f"{g['forca']:.2f}"
        print(f"{g['vezes']:>3}x  [forca {forca}]  {g['pergunta'][:80]}")
        print(f"      notas={[n for n, _ in g['notas']] or '—'} "
              f"tabelas={[t for t, _ in g['tabelas']][:6] or '—'} "
              f"sql={g['respostas']}")
    fila = lacunas(grupos=grupos)
    print(f"\n== lacunas ({len(fila)}) — `--dossie` gera o rascunho de cada uma ==")
    for g in fila[:15]:
        forca = "—" if g["forca"] is None else f"{g['forca']:.2f}"
        print(f"{g['motivo']:>16}  forca {forca}  {g['vezes']:>3}x  "
              f"{g['pergunta'][:70]}")
