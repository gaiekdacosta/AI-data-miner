---
titulo: "vw_faturamento_mensal é bruto: devoluções precisam ser abatidas à parte"
perfil: loja
tabelas: [vw_faturamento_mensal, devolucoes, itens_pedido]
pontes:
  - devolucoes -> itens_pedido (item_pedido_id)
data: 2026-01-15
---

# A view mensal não abate devolução

**Contexto:** "receita líquida de 2025" — a view mensal parecia pronta para
isso, mas devolve o faturamento bruto.

**Caminho:** `devolucoes` -> `itens_pedido` por `item_pedido_id`. A devolução
guarda só a QUANTIDADE devolvida; o valor sai do item (preço e desconto dele).

```sql
SELECT ROUND(SUM(d.quantidade * i.preco_unitario * (1 - i.desconto_pct / 100.0)), 2)
  FROM devolucoes d
  JOIN itens_pedido i ON i.id = d.item_pedido_id
```

**Lógica:** 191 devoluções em 2025, somando R$ 58.518,41. Líquido = bruto da
view (R$ 2.317.595,29) − devoluções = R$ 2.259.076,92. Toda devolução é de
pedido faturado.

**Armadilhas:**
- A data da devolução (`data_devolucao`) pode cair no mês seguinte ao do
  pedido: num corte mensal, decida antes se a devolução abate o mês da venda ou
  o mês em que voltou — o número muda.
- Ver [[faturamento-so-pedido-faturado]] para o filtro de status.
