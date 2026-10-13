---
titulo: "Valor vendido vem de itens_pedido.preco_unitario com desconto, nunca de produtos.preco_tabela"
perfil: loja
tabelas: [itens_pedido, produtos, categorias]
pontes:
  - itens_pedido -> produtos (produto_id)
  - produtos -> categorias (categoria_id)
data: 2026-01-15
---

# Valor vendido vem do item, não do cadastro

**Contexto:** "faturamento por categoria" — o primeiro rascunho multiplicava a
quantidade pelo preço do produto e o total não batia com a view mensal.

**Caminho:** `itens_pedido` -> `produtos` por `produto_id` -> `categorias` por
`categoria_id`. O preço que vale é o do ITEM: `itens_pedido.preco_unitario`, e o
desconto vem à parte em `itens_pedido.desconto_pct` (percentual, 0 a 15).

**Lógica:** `produtos.preco_tabela` é o preço de HOJE — foi reajustado depois
das vendas de 2025. O item guarda o preço praticado no dia. Em 2025, nos pedidos
faturados, quantidade × preço de tabela daria R$ 2.588.460,94; quantidade ×
preço do item, R$ 2.414.739,00; e, com o desconto, R$ 2.317.595,29 (o número
certo).

**Armadilhas:**
- 2.336 dos 5.517 itens têm desconto: esquecer o `desconto_pct` superestima em
  ~4%.
- `produtos.ativo = 0` (4 produtos) é produto fora de linha, não venda
  cancelada — ele continua aparecendo em pedido antigo, e filtrar por `ativo`
  num relatório de vendas apaga venda real.
