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

There are no test or lint commands configured.

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
    services.py     # Async service helpers and image-cache utilities:
                    #   _fetch_history_for_item, _enrich_prices (inventory enrichment)
                    #   _cache_images, _enrich_images_from_cache (image cache fill/lookup)
                    #   _register_skin, _register_flat (ByMykel static data registration)
                    #   _fetch_static_images (lazy loader for ByMykel/CSGO-API)
                    #   _build_movers_from_topmovers (hot/cold builder from market-index)
                    #   Constants: STEAM_WEB_API, _STATIC_*_URL, _WEAR_NAMES, _MOVERS_LIMIT
    router.py       # APIRouter: /me, /inventory, /market/movers, /market/trending,
                    #   /market/index, /item/history, /news/cs2
                    # Constants: _MOVERS_SELECT, _TRENDING_LIMIT
```

**Dependency order** (no circular imports):

```
settings.py, stores.py, middleware.py, steam/mappers.py  ← nothing internal
auth/service.py    ← stores, settings
auth/router.py     ← auth/service, stores, settings
steam/services.py  ← steam/mappers, stores, settings
steam/router.py    ← steam/services, steam/mappers, stores, settings, auth/service (require_jwt only)
main.py            ← middleware, auth/router, steam/router, settings
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
| GET | `/item/history` | Bearer | Item price history; `?name=<hash>&interval=<minutes>` |
| GET | `/news/cs2` | — | CS2 news via Steam News API; `?count=N` (default 5); rate-limited |

## Data mapping

steamwebapi.com responses are transformed in `steam/mappers.py` before being returned:

- `_map_item(item)` — inventory items → camelCase shape (`priceLatest`, `priceDelta24h`, `floatValue`, `phase`, `externalPrices`, etc.). Handles both flat `/inventory` and nested `/float/assets?with_items=1` formats.
- `_delta_from_history(pts, days, latest)` — computes % price change vs. N days ago from a history list. Used by `_enrich_prices`.
- `_map_market_index_point(point)` — time-series points → `{ date, price, change, volume }`
- `_map_news_item(item, index, image_url)` — Steam news items → normalized shape; `featured: true` for index 0; includes `content` excerpt via `_clean_news_content`
- `_fetch_og_image(client, url)` — async OG image scraper used by `/news/cs2`

**Inventory enrichment** (`steam/services.py`): after `_map_item` maps the static API data, `_enrich_prices` fires one concurrent `asyncio.gather` call to `/history` per item (hardcoded `interval=10` min). This overwrites `priceLatest`, `priceDelta24h`, `priceDelta7d`, `priceDelta30d` with live-history-derived values. Cached per item in `_item_history_cache`. This means a first `/inventory` call can hit the API N times (once per item) — relevant when near the 5 req/day free plan limit.


## In-memory stores (single-worker only)

All stores live in `stores.py`. **TODO:** replace with Redis before running multiple workers.

| Store | Key → Value | Purpose |
|-------|------------|---------|
| `_nonces` | nonce → (issued_at, redirect_origin) | CSRF protection for OpenID |
| `_auth_codes` | code → (steam_id, expires_at) | One-time codes (TTL 30 s) |
| `_refresh_store` | jti → expires_at | Refresh token revocation list |
| `_rate_store` | ip → [timestamps] | Sliding-window rate limiter |
| `_profile_cache` | steam_id → (data, cached_at) | 23 h cache — free plan: 5 req/day |
| `_inventory_cache` | steam_id → (data, cached_at) | 23 h cache |
| `_market_index_cache` | tf → (data, cached_at) | 23 h cache; keyed by timeframe |
| `_item_history_cache` | `name:interval` → (data, cached_at) | 23 h cache; shared by `/item/history` and `_enrich_prices` |

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

## Startup validation

On startup the lifespan hook warns if:
- `JWT_SECRET` equals the default placeholder `"change-this-secret"`
- `JWT_SECRET` is shorter than 32 characters
- `STEAM_API_KEY` is empty

The lifespan also creates a shared `httpx.AsyncClient` stored in `app.state.http_client` (closed on shutdown). All endpoints that call external services must use this client — never create per-request clients.

## Production checklist

Before any production deployment:

- `auth/service.py` `_set_refresh_cookie` and `auth/router.py` `logout`: `secure=False` → `secure=True`
- `.env`: `BASE_URL` and `FRONTEND_URL` → `https://` URLs
- `run_dev.py`: restore `ssl_certfile` / `ssl_keyfile` in uvicorn (or terminate TLS at a reverse proxy)
- Replace `stores.py` in-memory dicts with Redis before running multiple workers
