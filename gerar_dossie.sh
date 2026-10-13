#!/usr/bin/env bash
# Relatório de lacunas da base de conhecimento.
#
# Grava dossie/dossie-AAAA-MM-DD.{md,json}, que a tela /dossie do painel publica.
# Agende no host (cron ou systemd timer), por exemplo toda segunda às 6h30:
#
#   30 6 * * 1  cd /caminho/motor-consulta && ./gerar_dossie.sh
#
# Não escreve em NOTES/: a decisão de virar nota é de quem lê, e a verificação no
# banco continua obrigatória (a SQL registrada rodou, o que não quer dizer que a
# leitura dela estava certa).
#
# Roda DENTRO do container mcp de propósito: só ele monta contexto/ (pontuar a
# pergunta exige o índice vigente) e a pasta dossie/ — que o Docker criou como
# root na primeira subida, então o host nem sempre consegue escrever nela.
set -euo pipefail

cd "$(dirname "$0")"

# Container fora do ar não pode virar relatório vazio: falha e aparece no log do
# agendador.
if [ "$(docker compose ps -q mcp | wc -l)" -eq 0 ]; then
  echo "servico mcp nao esta no ar; nada gerado" >&2
  exit 1
fi

docker compose exec -T mcp python /app/perguntas.py "${DOSSIE_DIAS:-30}" \
  --recalcular --gravar /dossie --guardar "${DOSSIE_GUARDAR:-12}"
