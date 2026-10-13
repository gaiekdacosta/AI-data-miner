---
titulo: "Faturamento conta só pedido com status 'faturado' — cancelado e aberto continuam na tabela"
perfil: loja
tabelas: [pedidos, itens_pedido]
pontes:
  - pedidos -> itens_pedido (pedido_id)
data: 2026-01-15
---

# Faturamento conta só pedido faturado

**Contexto:** "quanto a loja faturou em 2025?" — e por que dois números
diferentes apareceram para a mesma pergunta.

**Caminho:** `pedidos` (cabeçalho, com `status` e `data_pedido`) ->
`itens_pedido` por `pedido_id`. O valor de cada item é
`quantidade * preco_unitario * (1 - desconto_pct / 100.0)`.

```sql
SELECT ROUND(SUM(i.quantidade * i.preco_unitario * (1 - i.desconto_pct / 100.0)), 2)
  FROM pedidos p
  JOIN itens_pedido i ON i.pedido_id = p.id
 WHERE p.status = 'faturado'
   AND p.data_pedido BETWEEN '2025-01-01' AND '2025-12-31'
```

**Lógica:** `status` tem três valores — `faturado` (1.972 pedidos em 2025),
`cancelado` (182) e `aberto` (46, todos de dezembro, ainda não faturados).
Faturamento é só o primeiro: R$ 2.317.595,29 em 2025. Bate com a soma da
`vw_faturamento_mensal`.

**Armadilhas:**
- Somar `itens_pedido` sem passar por `pedidos.status` dá R$ 2.590.770,23 —
  11,8% a mais, porque entram os cancelados e os abertos.
- O valor vendido NÃO é o preço do cadastro: ver [[valor-vendido-vem-do-item]].
- Faturamento bruto não é receita líquida: devolução fica fora da view, ver
  [[devolucao-nao-sai-da-view]].
