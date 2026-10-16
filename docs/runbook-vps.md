# Runbook del VPS (worker de precios)

Esqueleto de COST-01; COST-08 lo completa (alertas, `market_age_min`, incidencias).
El stack está en `deploy/compose.yml`: Valkey, TimescaleDB, worker (desde COST-02),
Uptime Kuma y **Nginx** como reverse proxy (no Caddy).

## Principios

- **Cero puertos públicos nuevos.** Solo Nginx publica, y solo en la IP de Tailscale.
  Valkey y Postgres viven en la red `internal` de Compose (sin salida a internet).
- **Secretos fuera del repo**: `/opt/csfinance/.env`, `chmod 600`, dueño root. Plantilla con
  los nombres en `deploy/.env.example`. La service key de Supabase solo existe en Render y aquí.
- No redimensionar el VPS (gama CX/CAX no disponible y con subidas en 2026).

## Estructura en disco

```
/opt/csfinance/
  .env            chmod 600
  app/            clon de LoginCsFinance (deploy/ dentro)
  tls/            <TS_HOSTNAME>.crt / .key de `tailscale cert`
  backups/        pg_dump diarios (7 días) + backup.log
```

## Instalación (una vez)

1. Tailscale: `tailscale up`; anotar `tailscale ip -4` (→ `TAILSCALE_IP`) y el nombre MagicDNS
   (→ `TS_HOSTNAME`). Activar HTTPS en el tailnet y emitir el certificado:
   `tailscale cert --cert-file /opt/csfinance/tls/$TS_HOSTNAME.crt --key-file /opt/csfinance/tls/$TS_HOSTNAME.key $TS_HOSTNAME`.
   Caduca a los 90 días: cron mensual que lo reemite y hace `docker compose restart nginx`.
2. `cp deploy/.env.example /opt/csfinance/.env && chmod 600 /opt/csfinance/.env` y rellenar.
3. Arrancar: `docker compose --env-file /opt/csfinance/.env -f deploy/compose.yml up -d`.
   El primer arranque de Timescale crea el rol `worker` y el esquema (`deploy/vps/initdb/`).
4. Backups: cron de root `15 3 * * * /opt/csfinance/app/deploy/vps/backup.sh >> /opt/csfinance/backups/backup.log 2>&1`.

### Checklist ufw / Tailscale

- [ ] `ufw default deny incoming` y `ufw default allow outgoing`.
- [ ] `ufw allow in on tailscale0` (todo lo del tailnet) y SSH **solo** por Tailscale
      (`ufw delete allow OpenSSH` cuando el acceso por tailnet esté probado).
- [ ] `ufw allow 41641/udp` (WireGuard directo de Tailscale; sin él va por DERP, más lento).
- [ ] ⚠️ Docker se salta ufw con los `ports:` publicados: por eso el único publish está atado a
      `${TAILSCALE_IP}` y nunca a todas las interfaces. Revisar `docker ps` tras cada cambio.
- [ ] Desde fuera del tailnet: `nmap -Pn -p- <IP pública>` → solo lo que ya había (SSH si sigue).

## Verificación (criterio de COST-01)

```bash
C="docker compose --env-file /opt/csfinance/.env -f deploy/compose.yml"
$C ps                                                    # todo healthy
docker ps --format '{{.Names}} {{.Ports}}'                # solo nginx con <TAILSCALE_IP>:443
$C exec valkey sh -c 'REDISCLI_AUTH=$VALKEY_PASSWORD valkey-cli ping'          # PONG
$C exec timescaledb psql -U postgres -d csfinance -c \
  "select hypertable_name from timescaledb_information.hypertables"             # market_quotes, market_ref
curl -s https://$TS_HOSTNAME/healthz                     # ok (desde el tailnet)
/opt/csfinance/app/deploy/vps/backup.sh                  # backup ok …
```

Probado en local el 2026-10-13 (Docker, `TAILSCALE_IP=127.0.0.1`, certificado autofirmado):
cuatro servicios healthy, 2 hypertables + 2 agregados continuos + 8 políticas (columnstore,
retención, refresco), `worker` sin permisos en `public` ni en la BD `postgres`, `FLUSHALL`
deshabilitado, `noeviction`, Valkey sin contraseña → `NOAUTH`, Nginx sirve `/healthz` y Kuma,
rechaza el handshake TLS con otro Host, y un dump se restaura entero. **Falta en el VPS real:**
el `nmap` desde fuera del tailnet y el acceso por la IP de Tailscale.

## Esquema

`deploy/vps/schema.sql` es idempotente. Para aplicar un cambio sin recrear el volumen:

```bash
$C exec -T timescaledb psql -U postgres -d csfinance -v ON_ERROR_STOP=1 -c "set role worker" -f - \
    < /opt/csfinance/app/deploy/vps/schema.sql
```

⚠️ **No usar `-f /schema/schema.sql`** (el fichero montado en el contenedor): `./vps/schema.sql` se monta
como **fichero**, y un `git pull` que lo cambie escribe un inode nuevo que el contenedor no ve. El
2026-10-16 así se aplicó una versión vieja sin `market_latest` y el worker nuevo falló dos ticks con
`UndefinedTableError`. El fichero del host por stdin es siempre el del repo. Comprobar al final con
`\d <tabla nueva>` o con la salida (`CREATE TABLE`, `INSERT 0 n`).

Cuando un cambio de código depende de un objeto nuevo del esquema, **primero el esquema y después
`up -d --build`**: el worker viejo ignora la tabla nueva, pero el nuevo falla sin ella. Ejemplo:
`market_latest` (2026-10-16): su relleno inicial desde `market_quotes` tarda ~1 min con 8,6 M de
filas y solo se ejecuta si la tabla está vacía. Comprobar después que `compute_reference` vuelve a
UP en Kuma en el siguiente cuarto de hora (2, 17, 32, 47).

## Restaurar un backup

El `pg_dump` avisa de FK circulares en el catálogo de Timescale: es normal, pero la restauración
tiene que ir entre `timescaledb_pre_restore()` y `timescaledb_post_restore()`:

```bash
$C exec -T timescaledb createdb -U postgres restaurada
$C exec -T timescaledb psql -U postgres -d restaurada -c "create extension timescaledb; select timescaledb_pre_restore();"
$C exec -T timescaledb pg_restore -U postgres -d restaurada < /opt/csfinance/backups/csfinance-AAAA-MM-DD.dump
$C exec -T timescaledb psql -U postgres -d restaurada -c "select timescaledb_post_restore();"
```

## Operación

- Logs: `$C logs -f --tail=200 <servicio>` (rotados a 3 × 10 MB por servicio).
- Actualizar: `git -C /opt/csfinance/app pull && $C up -d --build`.
- Uptime Kuma: `https://$TS_HOSTNAME:$NGINX_PORT/` desde el tailnet. Nueve monitores **push** (uno por job del
  worker más `noop` e `inventory`; intervalo 1,3–2,5× la cadencia, `maxretries` 1) y el de **disco** (abajo).
  COST-04 añade dos push más: `steam_topn` (cada hora, intervalo 7 200 s) y `steam_priceoverview` (cada
  5 min, 600 s); sus tokens van en `KUMA_PUSH_TOKENS` con esos nombres. Un 429 de Steam deja
  `steam_priceoverview` en DOWN mientras dure el cooldown (1 h, ×2 por reincidencia): es la señal, no un fallo.
- Alertas (COST-08): una notificación de Kuma (Telegram o email, creada en la UI con «Default enabled» y
  «Apply on all existing monitors») enlazada a todos los monitores. Comprobar el enlace en la sqlite, no en la
  UI: `docker exec csfinance-uptime-kuma-1 sqlite3 /app/data/kuma.db 'select count(*) from monitor_notification'`
  tiene que dar el número de monitores. Prueba: `$C stop worker`, esperar a que `noop` pase a DOWN (150 s de
  intervalo + 1 reintento ≈ 5 min), `$C start worker` → llegan el aviso de DOWN y el de UP.
- Disco (COST-08): `deploy/vps/disk-check.sh` empuja `up`/`down` al monitor push `disk` según `df /` supere el
  80 % (`DISK_MAX_PCT`). Lo lanza un cron de root cada 5 min (línea en la cabecera del script); el token va en
  `KUMA_PUSH_TOKENS` como `disk=<token>`. Si el disco ya está por encima al instalarlo, el primer aviso es real:
  la mitad del uso es de otros stacks del host (IRIS/n8n), no del worker.
