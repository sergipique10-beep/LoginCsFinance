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
python run_dev.py
# or directly:
uvicorn main:app --host 127.0.0.1 --port 8000 --reload

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
  run_dev.py        # Local dev launcher (uvicorn, HTTP only)
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
                    #   /internal/cap-tick. Constants: _MOVERS_SELECT, _TRENDING_LIMIT,
                    #   _CAP_TF_MAP, _CAP_BUCKET_MAP; helper _downsample.
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
| POST | `/notifications/register-token` | Bearer | Registra un token FCM (`{ token, platform }`) para recibir push notifications. |
| POST | `/internal/news-tick` | `X-News-Tick-Token` | Cron horario (GitHub Actions). Detecta noticias CS2 nuevas y envía push broadcast vía FCM. Idempotente (dedup por `gid` en `notified_news`). |
| POST | `/internal/broadcast` | `X-Broadcast-Token` | Anuncio manual (`workflow_dispatch` de GitHub Actions). Envía un push con `{title, body}` libres a todos los `device_tokens`. `data` vacío → al tocar, la app abre Home. No deduplica: no toca `notified_news`. Devuelve `{sent, failed, pruned}`. |
| GET | `/item/history` | Bearer | Item price history; `?name=<hash>&interval=<minutes>` |
| GET | `/news/cs2` | — | CS2 news via Steam News API; `?count=N` (default 5); rate-limited |

## Data mapping

steamwebapi.com responses are transformed in `steam/mappers.py` before being returned:

- `_map_item(item)` — inventory items → camelCase shape (`priceLatest`, `priceDelta24h`, `floatValue`, `phase`, `externalPrices`, etc.). Handles both flat `/inventory` and nested `/float/assets?with_items=1` formats.
- `_delta_from_history(pts, days, latest)` — computes % price change vs. N days ago from a history list. Used by `_enrich_prices`.
- `_map_market_index_point(point)` — time-series points → `{ date, price, change, volume }`
- `_map_news_item(item, index, image_url)` — Steam news items → normalized shape; `featured: true` for index 0; includes `content` excerpt via `_clean_news_content`
- `_fetch_og_image(client, url)` — async OG image scraper used by `/news/cs2`

**Price deltas** (`_inline_delta` in `steam/mappers.py`): deltas are computed from the **`pricereal` family** (`pricereal` vs `pricereal24h/7d/30d`). Do **not** use `pricelatestsell24h/7d/30d` — steamwebapi returns those identical to `pricelatestsell` for every item, so any delta derived from them is always `None` → `"N/A"` badges everywhere. `_inline_delta` also discards historical values more than 10× away from the current price (the API occasionally returns garbage, e.g. `pricereal30d=0.22` for a $17.57 skin → +7886%). `None` means no sales data and renders as `"N/A"`.

**History-derived enrichment** (`_enrich_prices` in `steam/services.py`): fires one concurrent `csfloat/history` call per item and overwrites the deltas with history-derived values. Cached per item in `_item_history_cache`. Used by `/market/*` (trending, movers) only — **not** by `/inventory`, which would need one API call per item and blow the 18/60s limiter. Inventory therefore relies entirely on `_inline_delta`.

**Rate limiting** (`_history_limiter`, `steam/services.py`): steamwebapi Starter allows **20 req/60s per endpoint**. Every `csfloat/history` call goes through a process-wide `_SlidingWindowLimiter` capped at 18/60s. Without it, bursts past 20 items got HTTP 429 → `_fetch_history_for_item` returns `[]` → `_delta_from_history` returns `None` → frontend renders `"N/A"` badges for every item past the 20th. Because the limiter makes callers *wait* rather than fail, the number of items enriched synchronously per request must fit one window — that's why `_TRENDING_LIMIT` and `_MOVERS_LIMIT` are ≤18.

**Liquidity Score** (`steam/liquidity.py`): `liquidityScore` (0-100) responde "si listo este ítem hoy, ¿en cuánto se vende y a qué precio real?". Cinco componentes ponderados: velocidad de ventas (0.30), tiempo de venta (0.25), haircut contra el mejor bid (0.25), buy orders en espera (0.10), consistencia entre mercados (0.10). Cuando falta un componente sus pesos se **renormalizan** entre los disponibles; si queda menos del 50% del peso, el score es `None` → `"N/A"` (misma convención que los price deltas). `compute_liquidity` recibe el dict **crudo** de steamwebapi, no el mapeado: `_map_item` colapsa `None` a `0` y eso borraría la diferencia entre "no hay datos" y "cero ventas". `_map_topmovers_item` devuelve `None` porque su payload no trae los campos. **`_MOVERS_SELECT` debe incluir `prices`**, o el Market renormaliza sobre 0.90 y el mismo ítem puntúa distinto que en el Inventario. Ver `docs/superpowers/specs/2026-07-14-liquidity-score-design.md`.

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
- `run_dev.py`: restore `ssl_certfile` / `ssl_keyfile` in uvicorn (or terminate TLS at a reverse proxy)
- Replace `stores.py` in-memory dicts with Redis before running multiple workers
