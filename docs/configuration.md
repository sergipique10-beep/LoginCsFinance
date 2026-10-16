# Configuration

See [Getting Started](./getting-started.md) for environment setup and certificate generation.

All backend settings are loaded from `.env` via `python-dotenv` (`settings.py`).

## Backend `.env`

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `BASE_URL` | Yes | `http://localhost:8000` | Public URL the backend is reachable at. Steam POSTs the OpenID callback here — must be publicly accessible (use ngrok in local dev). |
| `FRONTEND_URL` | Yes | `http://localhost:4200` | Angular app origin. Used for CORS and the post-login redirect. |
| `JWT_SECRET` | Yes | `change-this-secret` | Signs both access and refresh tokens. Use a strong random string in production. A warning is logged on startup if the default is detected. |
| `STEAM_API_KEY` | No | *(empty)* | Steam Web API key. Not required for login; reserved for future profile enrichment. Get one at https://steamcommunity.com/dev/apikey |
| `LEETIFY_API_KEY` | No | *(empty)* | Clave de la API pública de Leetify. Solo la usa el backend (`/me/stats*`); sin ella esos endpoints devuelven 503 (SEC-09). |
| `STEAM_GAME` | No | `"cs2"` | Game identifier sent to steamwebapi.com for inventory requests. Verify the exact slug accepted by your API plan — steamwebapi.com may require the numeric AppID `"730"` instead of `"cs2"`. A wrong value here produces a `502` from `GET /inventory`. |

Example `.env`:
```dotenv
BASE_URL=https://xxxx.ngrok-free.app
FRONTEND_URL=https://localhost:4200
JWT_SECRET=your-random-64-char-secret-here
STEAM_API_KEY=
```

## Segundo servicio de Render: `cs-finance-api-next` (COST-12)

Desde el 2026-10-16 hay **dos** web services en Render sobre el mismo repo:

| Servicio | Rama | URL | Para qué |
|---|---|---|---|
| `cs-finance-api` | `master` | `https://cs-finance-api.onrender.com` | Producción (APK publicado) |
| `cs-finance-api-next` | `feat/migracion-steamwebapi` | `https://cs-finance-api-next.onrender.com` | Rama de integración de la migración (COST-xx): sombra del inventario, pruebas con el APK `next` |

Mismo runtime (Python), build (`pip install -r requirements.txt`) y arranque (`uvicorn main:app --host 0.0.0.0`),
plan free. **`master` no recibe nada de la migración hasta el final**: la app cambia de backend tocando solo la
URL (`CS-FINANCE-ionic`: `npm run build:next` usa `environment.next.ts`); ningún endpoint se renombra.

Variables **iguales** a producción: `STEAM_API_KEY`, `STEAM_GAME`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
`GEMINI_API_KEY`, `FIREBASE_SERVICE_ACCOUNT_JSON`, `REVIEW_*`, `ENV=production`, `COOKIE_SECURE=true`,
`DEBUG=false`. Variables que **difieren**, y por qué:

| Variable | En `next` | Por qué |
|---|---|---|
| `BASE_URL` | URL del servicio `next` | Es el `return_to` del callback de Steam OpenID: con la de producción el login volvería al otro backend |
| `JWT_SECRET` | Distinto (`secrets.token_urlsafe(48)`) | Un token emitido por un backend no debe valer en el otro (verificado: 401 cruzado) |
| `CHAT_ENABLED` | `false` | La cuota de Gemini (20 req/día) es por proyecto de Google y se compartiría con producción |
| `CHAT_RAG_PRELOAD` | `false` | Sin chat, el precalentamiento del RAG solo gastaría embeddings |
| `INVENTORY_SOURCE`, `PROFILE_SOURCE` | `shadow` | Sirve steamwebapi y encola `fetch_requests` para que el worker del VPS rellene `inventory_snapshots_shadow` (COST-07) |
| `ALERTS_TICK_INTERVAL` | `0` | Supabase y Firebase son compartidos: el tick interno (PUSH-10) enviaría a los usuarios reales **cada alerta dos veces** |
| `PRICE_TICK_TOKEN`, `CAP_TICK_TOKEN`, `ALERTS_TICK_TOKEN`, `NEWS_TICK_TOKEN`, `RAG_INGEST_TOKEN`, `BROADCAST_TOKEN` | Ausentes | Con el token esperado vacío `token_matches` devuelve siempre `False`: los `/internal/*` quedan deshabilitados. `BACKEND_BASE_URL` de GitHub Actions sigue apuntando a producción; los precios nuevos los produce el worker del VPS |
| `LEETIFY_API_KEY` | Ausente | `/me/stats*` responde 503 en `next`; la clave está pendiente de rotación (SEC-09) |
| `FRONTEND_URL`, `ALLOWED_REDIRECT_ORIGINS` | `https://localhost` y `myapp://auth/callback` | Solo se usa desde el APK: el login Android redirige al esquema `myapp://`; `https://localhost` es el origen del WebView de Capacitor |
| `PYTHON_VERSION` | `3.12.7` | Fijada explícitamente (la del `venv` local) |

Avisos:

- **Supabase es compartido.** `DELETE /me` desde `next` borra `device_tokens`, `price_alerts` y `refresh_tokens`
  reales del usuario. Solo con la cuenta de pruebas (`REVIEW_*`).
- Con `INVENTORY_SOURCE=shadow` cada `GET /inventory` desde `next` gasta cuota de steamwebapi (es una sola
  para los dos backends). En `own` (tras COST-06), cero.
- Render free duerme: la primera petición tarda 30–50 s. No es un fallo.
- Tras cada deploy: `curl https://cs-finance-api-next.onrender.com/` → 200 y
  `curl -X POST https://cs-finance-api-next.onrender.com/auth/dev-token` → 404.

## Angular environment files

Two environment files control where HTTP calls go:

| File | Used when | `apiUrl` |
|------|-----------|----------|
| `environment.ts` | `ng serve` (dev web) | `/api` (proxied to `localhost:8000`) |
| `environment.prod.ts` | `ng build` / Android | `https://TBD` (dominio de producción por definir) |

The dev proxy target is set in `proxy.conf.json` (applies **only** to `ng serve`):

```json
{
  "/api": {
    "target": "https://localhost:8000",
    "secure": false,
    "pathRewrite": { "^/api": "" }
  }
}
```

For Android/native builds, the proxy is not involved — calls go directly to `environment.apiUrl`. See [steam-auth-angular.md](./steam-auth-angular.md#ionic--capacitor-android) for details.

## Rate limiting (hardcoded in `main.py`)

| Constant | Value | Meaning |
|----------|-------|---------|
| `RATE_LIMIT_CALLS` | 10 | Max requests per window |
| `RATE_LIMIT_WINDOW` | 60 s | Rolling window |
| `NONCE_TTL` | 300 s | How long a Steam OpenID nonce stays valid |
