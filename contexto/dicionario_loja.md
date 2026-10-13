# Dicionário de dados — Loja de exemplo (dados fictícios) — clientes, produtos, pedidos e devoluções

Gerado por gerar_dicionario.py a partir das tabelas citadas em NOTES/ e dos extras.

Dialeto: SQLite (LIMIT, IFNULL, strftime; tipos dinâmicos).

Consulte com `--perfil loja`.

Banco de EXEMPLO gerado por `exemplo/criar_banco.py`: loja fictícia de papelaria
e informática, ano de 2025, dados inventados. Valores em reais. Datas em texto
ISO (`AAAA-MM-DD`) — compare com `BETWEEN '2025-01-01' AND '2025-12-31'` e agrupe
com `strftime('%Y-%m', ...)`.


## Schema das tabelas e views conhecidas

### categorias (TABLE)

| Coluna | Tipo | Nulo | Comentário |
|---|---|---|---|
| id | INTEGER | Y | chave primária |
| nome | TEXT | N |  |

### clientes (TABLE)

| Coluna | Tipo | Nulo | Comentário |
|---|---|---|---|
| id | INTEGER | Y | chave primária |
| codigo | TEXT | N |  |
| cidade | TEXT | N |  |
| uf | TEXT | N |  |
| segmento | TEXT | N | varejo / empresa / governo |
| criado_em | DATE | N |  |

### devolucoes (TABLE)

| Coluna | Tipo | Nulo | Comentário |
|---|---|---|---|
| id | INTEGER | Y | chave primária |
| item_pedido_id | INTEGER | N |  |
| data_devolucao | DATE | N |  |
| quantidade | INTEGER | N |  |
| motivo | TEXT | N |  |

### itens_pedido (TABLE)

| Coluna | Tipo | Nulo | Comentário |
|---|---|---|---|
| id | INTEGER | Y | chave primária |
| pedido_id | INTEGER | N |  |
| produto_id | INTEGER | N |  |
| quantidade | INTEGER | N |  |
| preco_unitario | NUMERIC | N | preço praticado NA VENDA |
| desconto_pct | NUMERIC | N |  |

### pedidos (TABLE)

| Coluna | Tipo | Nulo | Comentário |
|---|---|---|---|
| id | INTEGER | Y | chave primária |
| cliente_id | INTEGER | N |  |
| data_pedido | DATE | N |  |
| status | TEXT | N | aberto / faturado / cancelado |
| canal | TEXT | N | loja / site / telefone |

### produtos (TABLE)

| Coluna | Tipo | Nulo | Comentário |
|---|---|---|---|
| id | INTEGER | Y | chave primária |
| sku | TEXT | N |  |
| nome | TEXT | N |  |
| categoria_id | INTEGER | N |  |
| preco_tabela | NUMERIC | N | preço ATUAL de cadastro (reajustado) |
| ativo | INTEGER | N |  |

### vw_faturamento_mensal (VIEW)

| Coluna | Tipo | Nulo | Comentário |
|---|---|---|---|
| mes | ? | Y |  |
| pedidos | ? | Y |  |
| faturamento_bruto | ? | Y |  |


## Conhecimento acumulado (NOTES)

As regras e armadilhas deste banco ficam em NOTES/loja/.
