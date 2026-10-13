#!/bin/sh
# pg_dump nocturno de la Timescale del worker (COST-01). Cron del host, como root:
#   15 3 * * * /opt/csfinance/app/deploy/vps/backup.sh >> /opt/csfinance/backups/backup.log 2>&1
# Formato custom (-Fc): se restaura con pg_restore. Conserva 7 días.
set -eu
DIR=${BACKUP_DIR:-/opt/csfinance/backups}
COMPOSE="docker compose --env-file ${CSFINANCE_ENV_FILE:-/opt/csfinance/.env} -f $(dirname "$0")/../compose.yml"
mkdir -p "$DIR"
umask 077
OUT="$DIR/csfinance-$(date +%F).dump"
$COMPOSE exec -T timescaledb pg_dump -U postgres -d csfinance -Fc > "$OUT.part"
mv "$OUT.part" "$OUT"
find "$DIR" -name 'csfinance-*.dump' -mtime +7 -delete
echo "$(date -Is) backup ok $(du -h "$OUT" | cut -f1) $OUT"
