# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Create and activate virtual environment (Windows)
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Start the server — HTTP, no TLS (dev only)
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload

# Health check
curl http://localhost:8000/
```

```bash
# Tests (usar el Python del venv: el del sistema no tiene firebase_admin)
venv\Scripts\python -m pytest tests/ -v
```

There is no lint command configured.

## Architecture

FastAPI microservice that authenticates users via **Steam OpenID 2.0** and issues two JWTs: a short-lived access token and a long-lived refresh token stored in an `HttpOnly` cookie.

**Auth flow:**
1. `GET /auth/steam` — rate-limited, issues a nonce, redirects to Steam OpenID
2. `GET /auth/steam/callback` — validates nonce + Steam response, extracts SteamID, emits a one-time auth code (TTL 30 s), redirects to `FRONTEND_URL/auth/callback?code=<code>`
3. `POST /auth/token` — consumes the one-time code, returns `{ access_token }` + sets `refresh_token` HttpOnly cookie
4. `POST /auth/refresh` — validates + rotates refresh token (JTI revocation), returns new `{ access_token }`
5. `POST /auth/logout` — revokes JTI, clears cookie

**Token claims:**

| Token | Claims | TTL |
|-------|--------|-----|
| Access | `sub` (SteamID), `type: "access"`, `aud: "cs-finance"`, `iat`, `exp` | 30 min |
| Refresh | `sub`, `type: "refresh"`, `jti`, `aud: "cs-finance"`, `iat`, `exp` | 7 days |

Both tokens are HS256. No separate `steam_id` claim — the SteamID is exclusively in `sub`.

## Module structure

```
LoginCsFinance/
  main.py           # App factory: lifespan, CORS, middleware, router registration (~60 lines)
  settings.py       # Env vars loaded via python-dotenv
  stores.py         # All in-memory stores and TTL constants (single point for Redis migration)
  middleware.py     # SecurityHeadersMiddleware
  data/             # (removed) formerly held market_cap_history.json — now in Supabase
  auth/
    service.py      # Auth helpers: _get_client_ip, _rate_limit, _issue_nonce, _consume_nonce,
                    #               _issue_tokens, _set_refresh_cookie, require_jwt
    router.py       # APIRouter: /auth/steam, /auth/steam/callback, /auth/token,
                    #            /auth/dev-token, /auth/refresh, /auth/logout
                    # Note: reads DEBUG via os.getenv() directly, not settings.py
  steam/
    mappers.py      # Pure data transformers: _map_item, _map_market_index_point,
                    #   _map_news_item, _fetch_og_image, _clean_news_content,
                    #   _delta_from_history, _best_price_from_markets, _safe_delta
    liquidity.py    # Liquidity Score (0-100): compute_liquidity. Puro, sin deps internas.
    services.py     # Async service helpers and image-cache utilities:
                    #   _fetch_history_for_item, _enrich_prices (inventory enrichment)
                    #   _cache_images, _enrich_images_from_cache (image cache fill/lookup)
                    #   _register_skin, _register_flat (ByMykel static data registration)
                    #   _fetch_static_images (lazy loader for ByMykel/CSGO-API)
                    #   _build_movers_from_topmovers (hot/cold builder from market-index)
                    #   Constants: STEAM_WEB_API, _STATIC_*_URL, _WEAR_NAMES, _MOVERS_LIMIT
    cap_history_repo.py  # Supabase data layer for the CS2 price-index history:
                    #   get_supabase (module-cached client, service_role),
                    #   insert_snapshot (upsert by ts), fetch_range (rows since cutoff).
                    #   supabase-py is sync → calls wrapped in asyncio.to_thread.
    routes/         # APIRouters split by domain (registered in routes/__init__.py):
      items.py      #   /me, /inventory, /item/history
      market.py     #   /market/movers, /market/items, /market/trending, /market/index,
                    #   /market/cap-history, /market/providers, /market/prices,
                    #   /internal/cap-tick, /internal/trending-tick,
                    #   /internal/enrich-tick, /internal/movers-tick.
                    #   Constants: _MOVERS_SELECT, _TRENDING_CAPTURE_LIMIT,
                    #   _ENRICH_BATCH, _TRENDING_STALE_DAYS, _CAP_TF_MAP,
                    #   _CAP_BUCKET_MAP; helper _downsample.
      news.py       #   /news/cs2
  notifications/
    repo.py           # Supabase data layer: device_tokens, notified_news (reuses steam/cap_history_repo's client)
    service.py        # register_token, send_broadcast (firebase-admin), check_and_notify_new_news
    router.py         # APIRouter: /notifications/register-token, /notifications/delete-token,
                      #            /internal/news-tick, /internal/broadcast
```

**Dependency order** (no circular imports):

```
settings.py, stores.py, middleware.py, steam/liquidity.py  ← nothing internal
auth/service.py         ← stores, settings
auth/router.py          ← auth/service, stores, settings
steam/mappers.py        ← steam/liquidity
steam/services.py       ← steam/mappers, stores, settings
steam/cap_history_repo.py ← settings (+ supabase)
steam/routes/*          ← steam/services, steam/mappers, steam/cap_history_repo, stores,
                          settings, auth/service (require_jwt only)
main.py                 ← middleware, auth/router, steam/routes, settings
```

## Endpoints

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| GET | `/` | — | Health check |
| GET | `/auth/steam` | — | Rate-limited; accepts `?platform=android` for Android redirect origin |
| GET | `/auth/steam/callback` | — | Validates nonce + Steam, emits one-time auth code |
| POST | `/auth/token` | — | Exchanges auth code → access token + refresh cookie |
| POST | `/auth/dev-token` | — | **Only active when `DEBUG=true`** — emits tokens without Steam |
| POST | `/auth/refresh` | cookie | Rotates refresh token |
| POST | `/auth/logout` | cookie | Revokes JTI, clears cookie |
| GET | `/me` | Bearer | Steam profile: `userName`, `avatarUrl`, `avatarThumbUrl`, `profileUrl`, `isOnline` |
| GET | `/inventory` | Bearer | Normalized CS2 inventory (see `steam/mappers.py:_map_item` + enrichment below) |
| GET | `/market/index` | Bearer | Market index: `turnover24h`, `sold24h`, `delta24h`, `hottestItem`, `history[]` |
| GET | `/market/cap-history` | Bearer | CS2 price-index history from Supabase, downsampled per `?tf=` (`7d`/`1m`/`3m`/`6m`/`1y`/`3y`). Returns `[{ ts, v, priceindex, realpriceindex, buyorderpriceindex, turnover24h }]`; `v = priceindex` (frontend contract). Invalid `tf` → 400. |
| POST | `/internal/cap-tick` | `X-Cap-Token` | Hourly capture (called by external cron). Fetches `market-index/cs2`, upserts an hour-floored snapshot of the 4 fields into Supabase. Token compared via `secrets.compare_digest`; bad/missing → 401. |
| POST | `/internal/trending-tick` | `X-Cap-Token` | Cron **horario** (`market-tick.yml`). Captura ~`_TRENDING_CAPTURE_LIMIT` items con 1 request a `/items` y hace **upsert por `name`** (no replace-all: borraría el enriquecimiento acumulado). Marca `seen_at` y purga las filas con >`_TRENDING_STALE_DAYS` sin aparecer. Devuelve `{ok, count, purged}`. |
| POST | `/internal/enrich-tick` | `X-Cap-Token` | Cron **cada 15 min** (`market-tick.yml`). Rueda progresiva: coge los `_ENRICH_BATCH` items con `enriched_at` más antiguo (nulls primero) y les recalcula los deltas desde `csfloat/history`. Escribe `enriched_at` **siempre**, con datos o sin ellos — si no, un item sin histórico acapararía la rueda para siempre. Devuelve `{ok, count, with_deltas}`. |
| POST | `/notifications/register-token` | Bearer | Registra un token FCM (`{ token, platform }`) para recibir push notifications. |
| POST | `/internal/news-tick` | `X-News-Tick-Token` | Cron horario (GitHub Actions). Detecta noticias CS2 nuevas y envía push broadcast vía FCM. Idempotente (dedup por `gid` en `notified_news`). |
| POST | `/internal/broadcast` | `X-Broadcast-Token` | Anuncio manual (`workflow_dispatch` de GitHub Actions). Envía un push con `{title, body}` libres a todos los `device_tokens`. `data` vacío → al tocar, la app abre Home. No deduplica: no toca `notified_news`. Devuelve `{sent, failed, pruned}`. |
| GET | `/item/history` | Bearer | Item price history; `?name=<hash>&interval=<minutes>` |
| GET | `/news/cs2` | — | CS2 news via Steam News API; `?count=N` (default 5); rate-limited |
| POST | `/rag/chat` | Bearer | Chat con el asistente Sharky (Gemini), con historial de turnos. Hace retrieval del RAG en cada mensaje (inyectado en el system prompt) y devuelve `reply` + `sources[]` (dedup por URL). Function calling multi-tool sobre `tools/` |
| POST | `/internal/rag-ingest` | `X-Rag-Ingest-Token` | Cron diario (GitHub Actions) de ingesta RSS + Steam News → embeddings Gemini → upsert en Supabase. Idempotente por `external_id` |

## Data mapping

steamwebapi.com responses are transformed in `steam/mappers.py` before being returned:

- `_map_item(item)` — inventory items → camelCase shape (`priceLatest`, `priceDelta24h`, `floatValue`, `phase`, `externalPrices`, etc.). Handles both flat `/inventory` and nested `/float/assets?with_items=1` formats.
- `_delta_from_history(pts, days, latest)` — computes % price change vs. N days ago from a history list. Used by `_enrich_prices`.
- `_map_market_index_point(point)` — time-series points → `{ date, price, change, volume }`
- `_map_news_item(item, index, image_url)` — Steam news items → normalized shape; `featured: true` for index 0; includes `content` excerpt via `_clean_news_content`
- `_fetch_og_image(client, url)` — async OG image scraper used by `/news/cs2`

**Price deltas** (`_inline_delta` in `steam/mappers.py`): deltas are computed from the **`pricereal` family** (`pricereal` vs `pricereal24h/7d/30d`). Do **not** use `pricelatestsell24h/7d/30d` — steamwebapi returns those identical to `pricelatestsell` for every item, so any delta derived from them is always `None` → `"N/A"` badges everywhere. `_inline_delta` also discards historical values more than 10× away from the current price (the API occasionally returns garbage, e.g. `pricereal30d=0.22` for a $17.57 skin → +7886%). `None` means no sales data and renders as `"N/A"`.

**History-derived enrichment** (`_enrich_prices` in `steam/services.py`): fires one concurrent `csfloat/history` call per item and overwrites the deltas with history-derived values. Cached per item in `_item_history_cache`. Used by `/market/*` (trending, movers) only — **not** by `/inventory`, which would need one API call per item and blow the 18/60s limiter. Inventory therefore relies entirely on `_inline_delta`.

**Rate limiting** (`_history_limiter`, `steam/services.py`): steamwebapi Starter allows **20 req/60s per endpoint**. Every `csfloat/history` call goes through a process-wide `_SlidingWindowLimiter` capped at 18/60s. Without it, bursts past 20 items got HTTP 429 → `_fetch_history_for_item` returns `[]` → `_delta_from_history` returns `None` → frontend renders `"N/A"` badges for every item past the 20th. Because the limiter makes callers *wait* rather than fail, the number of items enriched **in one pass** must fit one window — de ahí `_MOVERS_LIMIT` ≤18 y `_ENRICH_BATCH = 18`.

**Cuáles son los costes de verdad** — solo una llamada escala con el número de items:

| Llamada | Coste | ¿Escala? |
|---|---|---|
| `GET /items` (`max=_ITEMS_FETCH_MAX`) | 1 req | **No** — trae hasta 5000 items en una petición |
| `GET /{market}/prices` ×2 (`_enrich_market_prices`) | 2 req, cacheadas | **No** |
| `GET csfloat/history` (`_enrich_prices`) | 1 req **por item** | **Sí** ← el único |

Por eso el trending está partido en dos ticks: **captura** (`/internal/trending-tick`, horario, ~500 items por 1 req) y **enriquecimiento** (`/internal/enrich-tick`, cada 15 min, 18 items). Los items aún sin enriquecer conservan los deltas de `_inline_delta`, que ya vienen gratis en el payload de `/items` — ninguna tarjeta se queda sin badge, solo con un delta algo menos preciso hasta que le toque la rueda.

**Liquidity Score** (`steam/liquidity.py`): `liquidityScore` (0-100) responde "si listo este ítem hoy, ¿en cuánto se vende y a qué precio real?". Cinco componentes ponderados: velocidad de ventas (0.30), tiempo de venta (0.25), haircut contra el mejor bid (0.25), buy orders en espera (0.10), consistencia entre mercados (0.10).

## Predicción de precios (`predict/`)

La predicción es **determinista, no la hace el LLM**: se expone como tool y el agente solo redacta el resultado.

- **`predict/trend.py`** — regresión lineal OLS sin numpy. Devuelve estimación puntual **siempre acompañada de `intervalo`** (±1.96·σ residual, ensanchado por `sqrt(1+h/n)`), `tendencia`, `confianza`, `horizon_days` (1-30), `r2`, `model_version` (`linreg-ols-v1`) y `backtest`.
- **`predict/backtest.py`** — walk-forward: en cada corte t ajusta solo con `prices[:t]` y compara la proyección a H días contra el real en `t+H`. Compara MAE del modelo vs. el naive ("dentro de H días, el precio de hoy"). Requiere ≥5 cortes (`MIN_FOLDS`) para pronunciarse.
- **Gate**: si el backtest dice que **no supera a naive**, la confianza se fuerza a `"baja"` sea cual sea el R². La cifra se sigue devolviendo, pero declarada como poco fiable — el system prompt obliga al agente a advertirlo. En un random walk el modelo pierde contra naive y el gate salta (verificado en `tests/test_predict_trend.py`).
- **Fuente histórica** (`predict/service.py:_historico`): prioriza la serie propia de `precios_historicos` (≥20 puntos); si aún no hay suficientes o Supabase falla, cae a `_fetch_history_for_item` (CSFloat ~50d, con limiter y caché). Ambas devuelven la misma forma `[{date, price, volume}]`.

## Captura de precios por-skin (`steam/price_capture.py`)

Tablas `tracked_skins` (qué seguimos) y `precios_historicos` (la serie) — SQL en `docs/sql/precios_historicos.sql`. **Ojo: hay que ejecutarlo en Supabase; si la tabla no existe, la predicción cae silenciosamente a CSFloat.**

**Es la única relación declarada del esquema**: `precios_historicos.market_hash_name` → `tracked_skins.market_hash_name`, `ON DELETE CASCADE`. Formaliza lo que el código ya hacía — `capture()` inserta solo nombres que acaba de leer de `tracked_skins` — y cubre la ventana entre esa lectura y el upsert, que dura minutos por el `_history_limiter`. ⚠️ **Con `CASCADE`, cualquier limpieza que se añada a `tracked_skins` se lleva su serie histórica por delante.** Es lo deseable (dejar de seguir una skin debe borrar sus datos), pero conviene saberlo antes de escribir un `purge_stale` para skins como el que ya existe para `market_trending`.

Las tablas de ranking **no** tienen FK a `tracked_skins` a propósito: no todo ítem del ranking se sigue. Pero el `trending-tick` sí registra el **top `TRENDING_TRACK_TOP` por turnover** (`register_tracked(..., "trending")`), para que los ítems más relevantes acumulen serie propia en vez de que toda predicción sobre ellos caiga a CSFloat. Es best-effort: un fallo ahí no tumba la captura del ranking.

**La serie es UNA fila por skin y día** (`unique (market_hash_name, date)`). Dos consecuencias que gobiernan todo lo demás:

- **Correr el price-tick más veces al día no da más puntos** — refina el precio de hoy sobre la misma fila. Los 20 puntos que exige `predict/service.py` son 20 **días** de calendario, sin atajo. (Un backfill desde el histórico de CSFloat sí los daría de golpe; no está hecho.)
- **La skin que no entra en la corrida pierde el punto de ese día para siempre.** Por eso `PRICE_LOOKUP_CAP` (400) debe cubrir *toda* la población seguida, no rotarla: con 320 skins y cap 200 se quedaban 120 fuera cada día. Ojo, `tracked_skins` crece sola (270 de 320 vienen de `/inventory`, y ahora también del trending): si la población supera el cap, vuelven los huecos.

**El tick captura UN LOTE, no toda la población.** `PRICE_LOOKUP_CAP` (150) es el tamaño del lote; el workflow llama al endpoint **en bucle** hasta que el response trae `pendientes: 0`. Con ~391 skins son 3 llamadas de ~8 min.

⚠️ **El troceado es obligatorio, no una optimización: Render free corta las peticiones HTTP largas.** Medido en producción: 200 skins (~11 min) pasaba, 400 (~22 min) devolvió **502 y se perdió la corrida entera** — 0 puntos escritos ese día, porque `upsert_prices` va al final. Ampliar el `--max-time` del curl no arregla nada; quien cierra la conexión es el proxy de Render. `tests/test_price_cap_timeout.py` protege el tamaño del lote contra esa cota.

Dos invariantes del troceado, ambos load-bearing:

- **`fetch_tracked(limit, before=hoy)`** excluye las ya capturadas hoy (`or(last_captured.is.null, last_captured.lt.hoy)`). Sin ese filtro, cada lote devolvería el mismo conjunto y el bucle no avanzaría.
- **`mark_captured` marca las intentadas, no las capturadas.** Una skin que falla el lookup siempre (delistada, nombre inválido) quedaría "pendiente" para siempre y el bucle la reintentaría lote tras lote sin que `pendientes` bajara. Mismo criterio que `enrich_tick` con `enriched_at`.

`POST /internal/price-tick` (cron diario, `.github/workflows/price-tick.yml`) recorre las skins **menos-recientemente-capturadas primero** (`last_captured` asc, nulls primero) hasta `PRICE_LOOKUP_CAP`, hace lookup por-nombre vía el `_history_limiter` compartido, y hace upsert idempotente por `(market_hash_name, date)`. Best-effort: un fallo por skin no aborta la corrida. El seed inicial sale de `steam/data/tracked_seed.json`.

**Dónde vive**: solo en `/inventory` y `/market/items` (search) — los dos endpoints que sirven la salida de `_map_item`. **`/market/trending` y `/market/movers` NO lo llevan**: sirven snapshots de Supabase vía `_row_to_item` (`steam/market_rows.py`), que no lo transporta.

⚠️ **`liquidity_breakdown` NO puede añadirse a las tablas de ranking sin tocar el frontend a la vez.** El detail sheet (`skin-detail-sheet.component.ts`) detecta "esto es un snapshot pobre, pide el item completo a `/market/price`" con `liquidityBreakdown === undefined`, y es la única señal que le queda: el snapshot ya transporta volumen (`sold24h`, `offerVolume`, `hoursToSold`, `priceReal`, `steamUrl` — se añadieron para arreglar el `"Vol: undefined/24h"` que salía en todas las tarjetas). Si `_row_to_item` empieza a emitir `liquidityBreakdown`, el sheet deja de enriquecer **en silencio** y el bloque de liquidez queda vacío para siempre. El invariante está protegido por un assert en el self-check de `steam/market_rows.py` (`python -m steam.market_rows`).

**`_MOVERS_SELECT` debe incluir `prices`** — es load-bearing: `/market/items` sí calcula el score, y sin ese campo la consistencia entre mercados se descartaría ahí pero no en `/inventory`, con lo que el mismo ítem puntuaría distinto según la pantalla.

**Faltantes vs ceros** — la distinción de la que depende todo: `compute_liquidity` recibe el dict **crudo** de steamwebapi, no el mapeado, porque `_map_item` colapsa `None` a `0` (`d.get("sold24h") or 0`) y eso borraría la diferencia entre "no hay datos" (`None` → `"N/A"`) y "no se vende" (`0` → score bajo legítimo). Cuando un componente falta, su peso se **renormaliza** entre los disponibles. Cuidado con el corolario: **descartar un componente nunca puede hacer que un ítem parezca más líquido**. Por eso `buyorderprice == 0` ("nadie compra a ningún precio") es un dato real que da haircut `0.0`, no un faltante — tratarlo como faltante hacía que el ítem sin ningún comprador puntuara ~18 puntos MÁS ALTO que uno con un bid malo.

**Compuerta**: el score es `None` si el peso disponible es < 0.5 **o** si faltan a la vez `timeToSell` y `haircut` (`_CORE_COMPONENTS`) — sin tiempo ni precio, el número no responde la pregunta que dice responder.

`_map_topmovers_item` devuelve `None` porque su payload no trae los campos. Ver `docs/superpowers/specs/2026-07-14-liquidity-score-design.md`.

## In-memory stores (single-worker only)

All stores live in `stores.py`. **TODO:** replace with Redis before running multiple workers.

| Store | Key → Value | Purpose |
|-------|------------|---------|
| `_nonces` | nonce → (issued_at, redirect_origin) | CSRF protection for OpenID |
| `_auth_codes` | code → (steam_id, expires_at) | One-time codes (TTL 30 s) |
| `_refresh_store` | jti → expires_at | Refresh token revocation list |
| `_rate_store` | ip → [timestamps] | Sliding-window rate limiter |
| `_profile_cache` | steam_id → (data, cached_at) | 23 h cache — steamwebapi Starter: 20 req/60s per endpoint, 2k/day |
| `_inventory_cache` | steam_id → (data, cached_at) | 23 h cache |
| `_market_index_cache` | tf → (data, cached_at) | 23 h cache; keyed by timeframe |
| `_item_history_cache` | `name:interval` → (data, cached_at) | 23 h cache; shared by `/item/history` and `_enrich_prices` |

## Rankings de mercado (`market_trending` / `market_movers`)

DDL en **`docs/sql/market_rankings.sql`** — **hay que ejecutarla a mano en Supabase**; si faltan las columnas, los ticks fallan. Capa de datos: `steam/rankings_repo.py` (`RankingRepo`, una instancia por tabla).

Las dos tablas usan estrategias distintas y no hay que confundirlas:

- **`market_movers`** — replace-all (`replace_snapshot`: DELETE + INSERT). Son 20 items que se recalculan enteros cada 15 min, no acumulan nada.
- **`market_trending`** — upsert por `name` (`upsert_rows`). ~500 items capturados cada hora y enriquecidos en pasadas de 18 por un tick aparte. El DELETE no sirve aquí porque **borraría el enriquecimiento acumulado en cada captura**.

Del upsert salen tres consecuencias:

1. **`seen_at` + `purge_stale`** sustituyen al DELETE: sin ellos, un item que sale del ranking se quedaría en la tabla para siempre.
2. **`enriched_at`** es la rueda: `fetch_stalest` ordena por él, nulls primero. Mismo patrón que `last_captured` en `price_history_repo`.
3. **El orden ya no es `rank`** sino `turnover` desc (`fetch_ranked`): con upsert, un item que no aparece en una captura conserva el rank viejo y ocuparía una posición alta como fantasma. `rank` se sigue escribiendo, pero es informativo.

⚠️ **PostgREST rellena con `null` las claves que falten si las filas de un mismo lote no son homogéneas.** Por eso captura y enriquecimiento van en llamadas separadas, y `enrich-tick` hace además dos upserts (los que tienen deltas y los que no) — mezclarlos borraría los deltas buenos que `_inline_delta` dejó en la captura.

## Market cap history (Supabase, persistent)

The CS2 price-index history is **persisted in a dedicated Supabase Postgres project** (`cs-finance`), not in memory. This survives restarts/redeploys (ephemeral disk on Render free wiped the old JSON every deploy).

- **Table** `public.market_cap_history`: `ts timestamptz PK`, `priceindex` (not null), `realpriceindex`, `buyorderpriceindex`, `turnover24h`. RLS enabled with **no policies** — the backend uses the `service_role` key, which bypasses RLS.
- **Data layer**: `steam/cap_history_repo.py` (`get_supabase`, `insert_snapshot`, `fetch_range`). supabase-py is synchronous → all calls wrapped in `asyncio.to_thread`.
- **Capture**: an **external cron** (GitHub Actions, `.github/workflows/cap-tick.yml`, hourly at `:05`) POSTs `/internal/cap-tick`. This wakes the server even if it sleeps (Render free) — there is no in-process `asyncio` loop. The snapshot `ts` is floored to the hour so reruns within the same hour upsert (idempotent), not duplicate.
- **Serving**: `GET /market/cap-history?tf=` reads `fetch_range` and downsamples (`_downsample`) per `_CAP_BUCKET_MAP` (7d→1h, 1m→6h, 3m/6m→1d, 1y/3y→1w), averaging each field per bucket. Output keeps `{ ts, v }` with `v = priceindex`.
- **Note**: steamwebapi does not return past history (`history: []`) — the past is not backfilled, only captured better going forward.

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `BASE_URL` | `http://localhost:8000` | Must be reachable by Steam for the OpenID callback (use ngrok in local dev) |
| `FRONTEND_URL` | `http://localhost:4200` | CORS origin and post-login redirect target |
| `JWT_SECRET` | `change-this-secret` | Signs all tokens. Startup warns if default or < 32 chars. Use `secrets.token_urlsafe(48)` to generate. |
| `STEAM_API_KEY` | *(empty)* | Required for `/me`, `/inventory`, `/market/index`, `/item/history`. Startup warns if empty. |
| `STEAM_GAME` | `cs2` | Game ID passed to the steamwebapi.com inventory endpoint |
| `ALLOWED_REDIRECT_ORIGINS` | *(value of FRONTEND_URL)* | Comma-separated whitelist of allowed post-login redirect origins (add `myapp://` for Android) |
| `DEBUG` | `false` | Set `true` to activate `POST /auth/dev-token` |
| `SUPABASE_URL` | *(empty)* | URL of the `cs-finance` Supabase project. Startup warns if missing. |
| `SUPABASE_SERVICE_KEY` | *(empty)* | service_role key (bypasses RLS) — never the anon/publishable key. Startup warns if missing. |
| `CAP_TICK_TOKEN` | *(empty)* | Shared secret protecting `POST /internal/cap-tick`. Must match the GitHub Actions secret. Startup warns if missing. |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | *(empty)* | JSON completo de la service account de Firebase (Firebase Admin SDK), como string. Startup warns if missing. |
| `NEWS_TICK_TOKEN` | *(empty)* | Shared secret protecting `POST /internal/news-tick`. Must match the GitHub Actions secret. Startup warns if missing. |
| `BROADCAST_TOKEN` | *(empty)* | Shared secret protecting `POST /internal/broadcast` (anuncio manual). Token propio, **no** se reutiliza `NEWS_TICK_TOKEN`. Must match the GitHub Actions secret of the same name. Startup warns if missing. |
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | Modelo de embeddings de Gemini usado por el RAG (768 dims vía `outputDimensionality`) |
| `RAG_INGEST_TOKEN` | *(empty)* | Shared secret protecting `POST /internal/rag-ingest`. Must match the GitHub Actions secret. |
| `RAG_FEEDS` | `https://blog.counter-strike.net/index.php/feed/` | Feeds RSS a ingestar para el RAG, separados por coma |
| `RAG_MIN_SIMILARITY` | `0.5` | Similitud mínima (cosine, 0..1) para que un chunk cuente como fuente citable en `/rag/chat` |
| `CHAT_RAG_PRELOAD` | `true` | Precarga el contexto RAG en el system prompt de cada mensaje del chat (cuesta un embedding por turno). Con `false`, el modelo solo obtiene contexto si llama a la tool `buscar_contexto_rag` |
| `GEMINI_MODEL` | `gemini-flash-latest` | Modelo de generación del chat. El alias `-latest` resuelve a un modelo concreto que Google mueve sin avisar (hoy `gemini-3.5-flash`); en free tier la cuota es **20 req/día por proyecto y modelo**, así que conviene fijarlo a una versión explícita para que el gasto sea predecible. |
| `PRICE_TICK_TOKEN` | *(empty)* | Shared secret protecting `POST /internal/price-tick` (captura diaria de precios por skin). Must match the GitHub Actions secret. Startup warns if missing. |
| `PRICE_LOOKUP_CAP` | `150` | Skins por **lote** del price-tick (el workflow repite hasta `pendientes: 0`). ~8 min por lote. No subirlo sin más: a ~22 min Render free devuelve 502 y se pierde la corrida. |
| `TRENDING_TRACK_TOP` | `80` | Cuántos ítems del trending (top por turnover) se registran en `tracked_skins` en cada captura horaria. `0` desactiva el registro. Súbelo solo si `PRICE_LOOKUP_CAP` tiene margen sobre la población seguida. |

**Cuidado con los valores multilínea**: `python-dotenv` corta un valor que ocupe
varias líneas sin comillas — se queda con la primera. `FIREBASE_SERVICE_ACCOUNT_JSON`
cargaba como `"{"` y el guard `if not FIREBASE_SERVICE_ACCOUNT_JSON` no saltaba
(un `"{"` es truthy): el fallo aparecía después, en `json.loads`. Va en **una sola
línea entre comillas simples**, con los saltos de la private key como `\n` literales.

## Startup validation

On startup the lifespan hook warns if:
- `JWT_SECRET` equals the default placeholder `"change-this-secret"`
- `JWT_SECRET` is shorter than 32 characters
- `STEAM_API_KEY` is empty
- any of `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` / `CAP_TICK_TOKEN` is missing (cap-history won't work)

The lifespan also creates a shared `httpx.AsyncClient` stored in `app.state.http_client` (closed on shutdown). All endpoints that call external services must use this client — never create per-request clients.

## Production checklist

Before any production deployment:

- `auth/service.py` `_set_refresh_cookie` and `auth/router.py` `logout`: `secure=False` → `secure=True`
- `.env`: `BASE_URL` and `FRONTEND_URL` → `https://` URLs
- uvicorn: add `--ssl-certfile` / `--ssl-keyfile` (or terminate TLS at a reverse proxy)
- Replace `stores.py` in-memory dicts with Redis before running multiple workers
