#!/usr/bin/env python3
"""Gera o banco de exemplo: uma loja fictícia de papelaria e informática (SQLite).

Serve para ver o motor funcionando sem banco nenhum de verdade: o perfil ``loja``
do ``.env.example`` aponta para o arquivo gerado aqui, e as notas de
``NOTES/loja/`` ensinam as regras (e as armadilhas) deste schema.

Os dados são inventados e DETERMINÍSTICOS (semente fixa): rodar de novo gera o
mesmo arquivo, então os números citados nas notas continuam valendo. Clientes não
têm nome — só código, cidade e segmento —, como um banco bem cuidado faria.

Armadilhas plantadas de propósito (as notas de exemplo as documentam):

  - ``produtos.preco_tabela`` é o preço de HOJE; o que foi vendido está em
    ``itens_pedido.preco_unitario``, com o desconto à parte;
  - pedido cancelado continua na tabela (``status = 'cancelado'``);
  - a view ``vw_faturamento_mensal`` NÃO abate devoluções.

Uso:

    python3 exemplo/criar_banco.py                # gera exemplo/loja.db
    python3 exemplo/criar_banco.py /tmp/loja.db   # noutro caminho
"""

import datetime
import os
import random
import sqlite3
import sys

_AQUI = os.path.dirname(os.path.abspath(__file__))
SEMENTE = 20260101

CATEGORIAS = {
    "Papelaria": [("Caderno universitário 200 fls", 32.90), ("Caneta esferográfica (cx 50)", 54.00),
                  ("Papel A4 (resma 500 fls)", 29.90), ("Grampeador de mesa", 41.50),
                  ("Bloco adesivo 76x76 (pct 12)", 38.40), ("Pasta suspensa (cx 50)", 89.00)],
    "Informática": [("Mouse sem fio", 79.90), ("Teclado USB", 99.00), ("Monitor 24 pol", 899.00),
                    ("Pendrive 64 GB", 49.90), ("Headset com microfone", 159.00),
                    ("Webcam Full HD", 229.00), ("Hub USB-C 7 portas", 189.00)],
    "Móveis": [("Cadeira de escritório", 749.00), ("Mesa 120x60", 529.00),
               ("Gaveteiro volante", 389.00), ("Apoio para os pés", 119.00)],
    "Limpeza": [("Álcool 70% (5 L)", 44.90), ("Papel toalha (fardo)", 62.00),
                ("Detergente (cx 24)", 71.80), ("Saco de lixo 100 L (pct 100)", 58.50)],
    "Copa": [("Café em grãos 1 kg", 64.90), ("Copo descartável (cx 2500)", 96.00),
             ("Açúcar em sachê (cx 400)", 39.90), ("Filtro de papel (cx 30)", 9.90)],
    "Embalagens": [("Caixa de papelão M (pct 25)", 87.00), ("Fita adesiva (pct 10)", 46.00),
                   ("Plástico bolha (rolo 100 m)", 134.00), ("Envelope pardo (cx 250)", 79.00)],
}
CIDADES = [("São Paulo", "SP"), ("Campinas", "SP"), ("Belo Horizonte", "MG"),
           ("Curitiba", "PR"), ("Porto Alegre", "RS"), ("Florianópolis", "SC"),
           ("Rio de Janeiro", "RJ"), ("Salvador", "BA"), ("Recife", "PE"),
           ("Goiânia", "GO"), ("Fortaleza", "CE"), ("Manaus", "AM")]
SEGMENTOS = ["varejo", "varejo", "varejo", "empresa", "empresa", "governo"]
CANAIS = ["loja", "loja", "site", "site", "site", "telefone"]
MOTIVOS = ["defeito", "arrependimento", "item errado", "avaria no transporte"]

SCHEMA = """
CREATE TABLE categorias (
    id      INTEGER PRIMARY KEY,
    nome    TEXT NOT NULL UNIQUE
);
CREATE TABLE produtos (
    id            INTEGER PRIMARY KEY,
    sku           TEXT NOT NULL UNIQUE,
    nome          TEXT NOT NULL,
    categoria_id  INTEGER NOT NULL REFERENCES categorias(id),
    preco_tabela  NUMERIC NOT NULL,   -- preço ATUAL de cadastro (reajustado)
    ativo         INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE clientes (
    id         INTEGER PRIMARY KEY,
    codigo     TEXT NOT NULL UNIQUE,
    cidade     TEXT NOT NULL,
    uf         TEXT NOT NULL,
    segmento   TEXT NOT NULL,         -- varejo | empresa | governo
    criado_em  DATE NOT NULL
);
CREATE TABLE pedidos (
    id           INTEGER PRIMARY KEY,
    cliente_id   INTEGER NOT NULL REFERENCES clientes(id),
    data_pedido  DATE NOT NULL,
    status       TEXT NOT NULL,       -- aberto | faturado | cancelado
    canal        TEXT NOT NULL        -- loja | site | telefone
);
CREATE TABLE itens_pedido (
    id              INTEGER PRIMARY KEY,
    pedido_id       INTEGER NOT NULL REFERENCES pedidos(id),
    produto_id      INTEGER NOT NULL REFERENCES produtos(id),
    quantidade      INTEGER NOT NULL,
    preco_unitario  NUMERIC NOT NULL, -- preço praticado NA VENDA
    desconto_pct    NUMERIC NOT NULL DEFAULT 0
);
CREATE TABLE devolucoes (
    id               INTEGER PRIMARY KEY,
    item_pedido_id   INTEGER NOT NULL REFERENCES itens_pedido(id),
    data_devolucao   DATE NOT NULL,
    quantidade       INTEGER NOT NULL,
    motivo           TEXT NOT NULL
);
CREATE INDEX ix_pedidos_data ON pedidos(data_pedido);
CREATE INDEX ix_itens_pedido ON itens_pedido(pedido_id);
CREATE INDEX ix_devolucoes_item ON devolucoes(item_pedido_id);

-- Faturamento bruto por mês. NÃO abate devolução: é o número "da nota fiscal".
CREATE VIEW vw_faturamento_mensal AS
SELECT strftime('%Y-%m', p.data_pedido) AS mes,
       COUNT(DISTINCT p.id)             AS pedidos,
       ROUND(SUM(i.quantidade * i.preco_unitario * (1 - i.desconto_pct / 100.0)), 2)
                                        AS faturamento_bruto
  FROM pedidos p
  JOIN itens_pedido i ON i.pedido_id = p.id
 WHERE p.status = 'faturado'
 GROUP BY strftime('%Y-%m', p.data_pedido);
"""


def gerar(caminho):
    rnd = random.Random(SEMENTE)
    if os.path.exists(caminho):
        os.remove(caminho)
    conn = sqlite3.connect(caminho)
    conn.executescript(SCHEMA)

    # categorias e produtos — o preço de tabela foi reajustado depois das vendas
    produtos = []          # (id, preco_na_epoca)
    pid = 0
    for cid, (categoria, itens) in enumerate(CATEGORIAS.items(), start=1):
        conn.execute("INSERT INTO categorias VALUES (?, ?)", (cid, categoria))
        for nome, preco in itens:
            pid += 1
            reajuste = rnd.choice([1.00, 1.05, 1.08, 1.12])
            ativo = 0 if rnd.random() < 0.08 else 1
            conn.execute("INSERT INTO produtos VALUES (?, ?, ?, ?, ?, ?)",
                         (pid, f"SKU-{cid:02d}{pid:03d}", nome, cid,
                          round(preco * reajuste, 2), ativo))
            produtos.append((pid, preco))

    # clientes — só código, cidade e segmento
    inicio = datetime.date(2025, 1, 1)
    for i in range(1, 181):
        cidade, uf = rnd.choice(CIDADES)
        criado = inicio - datetime.timedelta(days=rnd.randint(0, 900))
        conn.execute("INSERT INTO clientes VALUES (?, ?, ?, ?, ?, ?)",
                     (i, f"C{i:04d}", cidade, uf, rnd.choice(SEGMENTOS), criado.isoformat()))

    # pedidos de 2025, com itens e devoluções
    item_id = dev_id = 0
    for ped in range(1, 2201):
        data = inicio + datetime.timedelta(days=rnd.randint(0, 364))
        sorteio = rnd.random()
        if data.month == 12 and sorteio < 0.25:
            status = "aberto"
        elif sorteio < 0.09:
            status = "cancelado"
        else:
            status = "faturado"
        conn.execute("INSERT INTO pedidos VALUES (?, ?, ?, ?, ?)",
                     (ped, rnd.randint(1, 180), data.isoformat(), status, rnd.choice(CANAIS)))
        for prod_id, preco in rnd.sample(produtos, rnd.randint(1, 4)):
            item_id += 1
            qtd = rnd.choice([1, 1, 1, 2, 2, 3, 5, 10])
            desconto = rnd.choice([0, 0, 0, 0, 5, 10, 15])
            conn.execute("INSERT INTO itens_pedido VALUES (?, ?, ?, ?, ?, ?)",
                         (item_id, ped, prod_id, qtd, preco, desconto))
            if status == "faturado" and rnd.random() < 0.04:
                dev_id += 1
                quando = data + datetime.timedelta(days=rnd.randint(2, 30))
                conn.execute("INSERT INTO devolucoes VALUES (?, ?, ?, ?, ?)",
                             (dev_id, item_id, quando.isoformat(),
                              rnd.randint(1, qtd), rnd.choice(MOTIVOS)))
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return caminho


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    caminho = argv[0] if argv else os.path.join(_AQUI, "loja.db")
    gerar(caminho)
    conn = sqlite3.connect(caminho)
    totais = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("categorias", "produtos", "clientes", "pedidos",
                        "itens_pedido", "devolucoes")}
    conn.close()
    print(f"OK — {caminho}: " + ", ".join(f"{n} {t}" for t, n in totais.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
