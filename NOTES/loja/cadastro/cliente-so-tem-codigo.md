---
titulo: "Cliente não tem nome no banco: recorte por cidade, UF e segmento"
perfil: loja
tabelas: [clientes, pedidos]
pontes:
  - pedidos -> clientes (cliente_id)
data: 2026-01-15
---

# Cliente só tem código

**Contexto:** "quem são os maiores clientes?" — não há nome para mostrar.

**Caminho:** `pedidos` -> `clientes` por `cliente_id`. O cliente é identificado
por `clientes.codigo` (C0001...) e descrito por `cidade`, `uf` e `segmento`
(`varejo`, `empresa`, `governo`).

**Lógica:** o cadastro não guarda nome nem documento de propósito. Para ranking,
mostre o código; para análise, agregue por `uf` ou `segmento`.

**Armadilhas:**
- `clientes.criado_em` é a data de cadastro, não a da primeira compra — a
  primeira compra é `MIN(pedidos.data_pedido)` do cliente.
