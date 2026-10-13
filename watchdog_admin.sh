#!/bin/sh
# Vigia o painel de acessos (admin_app.py) e o traz de volta.
#
# Por que existe, se o serviço já tem `restart: always`:
#   - o Docker NÃO reinicia container **unhealthy** (só o Swarm faz isso), então
#     "de pé e travado" ficaria fora do ar indefinidamente;
#   - `restart: always` não ressuscita o que o `compose down` removeu.
#
# Agende no host a cada poucos minutos (cron ou systemd timer):
#
#   */2 * * * *  /caminho/motor-consulta/watchdog_admin.sh
#
# DISTINGUIR 503 DE "SEM RESPOSTA" É O PONTO DO SCRIPT. O /saude devolve 503
# quando o tokens.caddy fica ilegível ou o socket do proxy desaparece — nesse caso
# o painel ESTÁ no ar, funcionando, e avisando que o problema é noutro serviço.
# Reiniciar o painel não consertaria nada e viraria loop de restart durante
# qualquer manutenção do proxy. Só "sem resposta" (000) e erro do próprio app
# (5xx que não 503) justificam reiniciar.
set -u

URL="${ADMIN_SAUDE_URL:-http://127.0.0.1:${ADMIN_PORTA:-8012}/saude}"
cd "$(dirname "$0")" || exit 1

codigo() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL" 2>/dev/null; }

c=$(codigo)
[ "$c" = "200" ] && exit 0

if [ "$c" = "503" ]; then
    echo "painel NO AR reportando degradado (HTTP 503): $(curl -s --max-time 5 "$URL")"
    echo "não reinicio o painel — o problema não é dele"
    exit 0
fi

# Falha isolada (deploy em andamento, pico) não justifica reiniciar.
sleep 10
c=$(codigo)
[ "$c" = "200" ] && { echo "painel oscilou e voltou sozinho — nada feito"; exit 0; }
[ "$c" = "503" ] && { echo "painel no ar, degradado (503) — nada feito"; exit 0; }

if docker compose ps --status running --services 2>/dev/null | grep -qx admin; then
    echo "painel de pé e sem responder (HTTP $c) -> restart"
    docker compose restart admin
else
    echo "container do painel ausente ou parado (HTTP $c) -> up -d"
    docker compose up -d admin
fi

sleep 15
c=$(codigo)
if [ "$c" = "200" ] || [ "$c" = "503" ]; then
    echo "painel recuperado (HTTP $c)"
else
    echo "painel SEGUE fora depois da tentativa (HTTP $c)"
    exit 1
fi
