# CLAUDE.md — LoginCsFinance

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
uvicorn main:app --host 127.0.0.1 --port 8001 --reload

# Health check
curl http://localhost:8001/
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
6. `GET /me` — protected route; returns `{ "steam_id": user["sub"] }`

**Token claims:**

| Token | Claims | TTL |
|-------|--------|-----|
| Access | `sub` (SteamID), `type: "access"`, `aud: "cs-finance"`, `iat`, `exp` | 30 min |
| Refresh | `sub`, `type: "refresh"`, `jti`, `aud: "cs-finance"`, `iat`, `exp` | 7 days |

Both tokens are HS256. No separate `steam_id` claim — the SteamID is exclusively in `sub`.

## Key files

| File | Role |
|------|------|
| `main.py` | All app logic: endpoints, middleware, token helpers, in-memory stores |
| `settings.py` | Loads env vars from `.env` via `python-dotenv` |
| `run_dev.py` | Local dev launcher (uvicorn, HTTP only) |
| `.env` | Local secrets — never committed |
| `docs/` | Full technical documentation |

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `BASE_URL` | `http://localhost:8001` | Must be reachable by Steam for the OpenID callback (use ngrok in local dev) |
| `FRONTEND_URL` | `http://localhost:4200` | CORS origin and post-login redirect target |
| `JWT_SECRET` | `change-this-secret` | Signs all tokens. Startup warns if default or < 32 chars. Use `secrets.token_urlsafe(48)` to generate. |
| `STEAM_API_KEY` | *(empty)* | Imported in `main.py`. Startup warns if empty. Required for all Steam Web API proxy endpoints. |
| `ALLOWED_REDIRECT_ORIGINS` | *(value of FRONTEND_URL)* | Comma-separated whitelist of allowed post-login redirect origins (add `myapp://` scheme for Android) |

## In-memory stores (single-worker only)

| Store | Key → Value | Purpose |
|-------|------------|---------|
| `_nonces` | nonce → (issued_at, redirect_origin) | CSRF protection for OpenID |
| `_auth_codes` | code → (steam_id, expires_at) | One-time codes (TTL 30 s) |
| `_refresh_store` | jti → expires_at | Refresh token revocation list |
| `_rate_store` | ip → [timestamps] | Sliding-window rate limiter |

**TODO:** Replace all four stores with Redis (TTL-native) before running multiple workers.

## Startup validation

On startup the lifespan hook warns if:
- `JWT_SECRET` equals the default placeholder `"change-this-secret"`
- `JWT_SECRET` is shorter than 32 characters
- `STEAM_API_KEY` is empty

The lifespan also creates a shared `httpx.AsyncClient` stored in `app.state.http_client` (closed on shutdown). All endpoints that call external services must use this client — do not create per-request clients.

## Production checklist

Before any production deployment, revert these dev shortcuts:

- `main.py` `_set_refresh_cookie` and `logout`: `secure=False` → `secure=True`
- `.env`: `BASE_URL` and `FRONTEND_URL` → `https://` URLs
- `run_dev.py`: restore `ssl_certfile` / `ssl_keyfile` in uvicorn (or terminate TLS at a reverse proxy)
