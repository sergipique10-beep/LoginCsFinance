#!/bin/sh
# Monitor de disco del host (COST-08): push a Uptime Kuma. Cron del host, como root, cada 5 min:
#   */5 * * * * /opt/csfinance/app/deploy/vps/disk-check.sh >> /opt/csfinance/backups/disk.log 2>&1
# El monitor `disk` (tipo push, intervalo 600 s = 2x el cron) se crea en la UI de Kuma; su token
# va en KUMA_PUSH_TOKENS del .env como `disk=<token>` (el worker ignora los jobs que no conoce).
# El push va por el curl del contenedor de Kuma: no depende de Nginx, del DNS del tailnet ni del TLS.
set -eu
ENV_FILE=${CSFINANCE_ENV_FILE:-/opt/csfinance/.env}
MAX=${DISK_MAX_PCT:-80}
MOUNT=${DISK_MOUNT:-/}
TOKEN=$(sed -nE 's/^KUMA_PUSH_TOKENS=(.*,)?disk=([^,]*).*/\2/p' "$ENV_FILE")
[ -n "$TOKEN" ] || { echo "$(date -Is) disk: falta disk=<token> en KUMA_PUSH_TOKENS de $ENV_FILE"; exit 1; }
PCT=$(df --output=pcent "$MOUNT" | tail -1 | tr -dc 0-9)
if [ "$PCT" -gt "$MAX" ]; then STATUS=down; else STATUS=up; fi
MSG="$MOUNT ${PCT}% usado (umbral ${MAX}%)"
COMPOSE="docker compose --env-file $ENV_FILE -f $(dirname "$0")/../compose.yml"
# ping=<%>: Kuma lo dibuja como "latencia"; aquí es el porcentaje ocupado, para ver la tendencia.
$COMPOSE exec -T uptime-kuma curl -fsS -G "http://127.0.0.1:3001/api/push/$TOKEN" \
    --data-urlencode "status=$STATUS" --data-urlencode "msg=$MSG" --data-urlencode "ping=$PCT" >/dev/null
echo "$(date -Is) disk $STATUS $MSG"
