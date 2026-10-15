---
feature: worker
files:
  - worker/settings.py
  - worker/logs.py
  - worker/broker.py
  - worker/jobs/__init__.py
  - worker/jobs/market_sync.py
  - worker/models.py
  - worker/pricing/reference.py
  - worker/adapters/_common.py
  - worker/adapters/skinport.py
  - worker/adapters/csgotm.py
  - worker/adapters/csgotrader.py
  - worker/adapters/catalog.py
  - worker/adapters/fx.py
  - worker/sources/_fetch.py
  - worker/sources/skinport.py
  - worker/sources/csgotm.py
  - worker/sources/csgotrader.py
  - worker/sources/catalog.py
  - worker/sources/fx.py
  - worker/store/market.py
  - worker/store/inventory.py
  - worker/jobs/inventory.py
  - worker/decode/certificate.py
  - worker/decode/inventory.py
  - worker/sources/steam_inventory.py
  - worker/sources/steam_profile.py
  - worker/main.py
  - worker/health.py
  - worker/net/gate.py
  - worker/net/breaker.py
  - worker/net/retry.py
  - worker/store/redis.py
  - worker/store/pg.py
  - worker/store/supabase.py
---

# Worker del VPS (COST-02)

Proceso aparte del backend de Render: corre en el VPS (`deploy/compose.yml`) y es quien
sustituye a steamwebapi recogiendo precios e inventarios de fuentes gratuitas. **No importa
nada del backend** (la imagen solo lleva `worker/`) y el backend no importa nada de aquí.

## Piezas

| Módulo | Qué hace |
|---|---|
| `broker.py` | Taskiq: `RedisStreamBroker` (streams + consumer group + ack) y `TaskiqScheduler` con `LabelScheduleSource`. Recursos por proceso en `WORKER_STARTUP` (`state.http`, `.redis`, `.gate`, `.breakers`, `.pg`, `.supabase`) |
| `jobs/` | `noop` (cada minuto: `SELECT 1` en Supabase y Timescale + push a Kuma) y `market_sync.py` (abajo). `jobs/__init__.py` importa los módulos hermanos para registrarlos |
| `main.py` | `python -m worker.main worker\|scheduler` → CLI de Taskiq. **Un único `scheduler`** |
| `net/gate.py` | GCRA en Valkey por ámbito (`rl:{scope}`: el host o `host/prefijo` si está declarado, `Gate.scope(url)`), jitter, cooldown tras 429 (`cool:{scope}`: 1 h, ×2, tope 6 h) y presupuestos diarios (`budget:{kind}:{yyyymmdd}`) |
| `net/breaker.py` | pybreaker por host con estado en Valkey (`cb:{host}`): 5 fallos → abierto 300 s (Steam 900). Logs `[breaker]` |
| `net/retry.py` | `request()`: **la única puerta a Internet**. Gate → breaker → petición → stamina (3 intentos en 5xx / transporte / 429 sin Retry-After fuera de Steam) |
| `store/` | Clientes de Valkey (async + sync, reconectan con backoff), Timescale (asyncpg) y Supabase (service_role) |
| `health.py` | Push a Uptime Kuma al final de cada job (`KUMA_PUSH_TOKENS=job=token,…`) |
| `logs.py` | `SecretFilter`: ningún secreto (`STEAM_WEB_API_KEY` y demás de `settings.SECRET_ENV`) ni `key=` de una URL en claro |

## market-sync (COST-03)

Fuentes gratuitas → `market_quotes` (Timescale) → precio de referencia → `market_ref` y Supabase
`market_current` (DDL `docs/sql/market_current.sql`, **tabla sombra** hasta COST-06).

| Job | Cron (UTC) | Fuente | `source` en market_quotes |
|---|---|---|---|
| `skinport_items` | `*/5` | Skinport `/v1/items` (br) | `skinport` (min_price, quantity) |
| `skinport_sales` | `*/15` | Skinport `/v1/sales/history` (br) | `skinport_sales` (mediana 24 h o 7 d, ventas 24 h) |
| `csgotm_prices` | `*/15` | market.csgo `prices/USD.json` | `marketcsgo` |
| `csgotrader_buff` | `:07` | csgotrader `buff163.json`, If-None-Match | `buff` — **solo si `BUFF_CURRENCY` es USD o CNY** |
| `fx_daily` | `06:05` | frankfurter USD→CNY | Valkey `fx:usd_per_cny` |
| `catalog_weekly` | lun `04:13` | ByMykel | `catalog_items` |
| `compute_reference` | `2,17,32,47` | — | `market_ref` + upsert de filas cambiadas a `MARKET_CURRENT_TABLE` |

- `pricing/reference.py` (puro): `w = log1p(qty) × 0.5^(edad_h/6) × k`, fuera lo que cae fuera de
  `[m/2, 2m]` (m = mediana simple), mediana ponderada. `ref_confidence` = fuentes que sobreviven
  (tope 3). `qty` = ventas, si no ofertas, mínimo 1. `k` por fuente en `PRICE_K` (JSON), 1,0 por
  defecto hasta calibrarlos en la convivencia.
- «Solo filas cambiadas»: sha256 de la fila **sin marcas de tiempo** (si no, todas cambiarían en
  cada tick) en el hash de Valkey `mc:hash`. El log del job da `changed_pct` (< 20 % esperado).
- Cada job deja su fila en `source_health` (`consecutive_failures`) y empuja a Kuma con su nombre.
- La moneda de Buff no se supone: sin `BUFF_CURRENCY` el job falla visible («cifra de COST-00»).
- Skinport: pedir confirmación escrita de redistribución antes del corte; `market_current` no
  publica el feed crudo, solo la referencia y el resumen por fuente.

## Inventario y perfil (COST-07)

Proceso aparte (`python -m worker.main inventory`, servicio `inventory` del compose, **uno**):

- Render inserta en Supabase `fetch_requests(kind, steam_id, prio)` (`docs/sql/fetch_requests.sql`).
- `ingress` (cada 3 s): `pending` → ZSET `inv:q` (score `prio·1e13 + ms`, miembro `{kind}:{steam_id}`,
  `ZADD LT`: la misma petición repetida se queda con la prioridad más alta) → `queued`.
- `fetcher` (una corutina): `ZPOPMIN` → candado `inv:inflight:{miembro}` 5 min → descarga → cierra
  **todas** las peticiones abiertas de ese (kind, steam_id) en `done` o `error` (`private`,
  `rate_limit`, `http_4xx`, `upstream`). Render decide el status HTTP con ese código.
- `inventory`: `steamcommunity.com/inventory` (con `Referer`, paginado, por el gate) →
  `decode/inventory.extract()` (float y patrón de `asset_properties`; fase Doppler del
  `paintindex` del certificado) → Supabase `INVENTORY_SNAPSHOT_TABLE` (**`inventory_snapshots_shadow`**
  hasta el corte; assets SIN precio) + VPS `inventory_assets`. Certificados en Valkey
  `inv:cert:{assetid}` 7 d + `asset_decoded`. Log con `decode_missing_ratio`.
- `profile`: `GetPlayerSummaries` (`STEAM_WEB_API_KEY`) → Supabase `steam_profiles`.
- `delete`: borra `inventory_assets` del usuario (Render no llega a la Timescale del VPS).
- Decodificador (`decode/certificate.py`): lector protobuf mínimo en vez de `econ_pb2.py` generado.

## Reglas

- **Ninguna petición a Internet sin `retry.request()`.** Un `httpx.get` directo se salta el
  gate y es como Steam acaba bloqueando la IP del VPS.
- **Un host sin entrada en `settings.DEFAULT_LIMITS` no pasa** (`UnknownHost`): un host nuevo
  se declara con su límite, no se cuela con uno por defecto.
- **Un 429 de Steam no se reintenta**: abre cooldown y todas las peticiones a ese host fallan
  rápido (`HostCoolingDown`) hasta que caduca. Un 429 nunca cuenta como caída para el breaker.
- Los límites del diseño (Steam Community 10/min ráfaga 2, Web API 60/min, Skinport 8/5 min)
  son el punto de partida; **se ajustan con las cifras de COST-00** vía `GATE_LIMITS` (JSON)
  en el `.env`, sin tocar código.
- **Un mismo host puede tener límites distintos por ruta.** Medido en COST-00 (2026-10-15):
  el inventario de `steamcommunity.com` corta a ~10 peticiones por ventana y bloquea ≤ 10 min,
  mientras `priceoverview` aguanta 10/min sin 429. Por eso `steamcommunity.com/inventory` tiene
  su propia entrada (6 por 10 min, ráfaga 2): cubo y cooldown aparte, breaker compartido con el
  host. Una clave `host/prefijo` en `GATE_LIMITS` declara otro ámbito igual.
- Garantía de la cola: *at-least-once*. Un job encolado durante un corte de Valkey puede
  tardar hasta `idle_timeout` (10 min) en reentregarse; los jobs tienen que ser idempotentes.

## Dependencias

`deploy/worker/requirements-worker.in` (directas) → `requirements-worker.txt` (con hashes,
generado en `python:3.12-slim` con `pip-compile --generate-hashes`; la imagen instala con
`--require-hashes`). CI las instala vía `requirements-dev.txt` para testear `worker/`.

## Verificación hecha (2026-10-13, Docker local)

`noop` cada minuto (scheduler encola, worker ejecuta, Timescale responde); Valkey parado 40 s
y arrancado → el worker reconecta sin reiniciar el contenedor. Tests: GCRA compartido por dos
procesos del SO contra un Redis real, cooldown, presupuestos, breaker compartido entre
instancias, política de reintentos. **Pendiente en el VPS:** push real a Kuma (monitor creado),
Supabase con la service key, y cortar la red 5 min para ver `[breaker]` abrir y cerrar.
