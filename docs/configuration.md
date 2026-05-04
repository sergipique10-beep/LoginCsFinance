# Configuration

See [Getting Started](./getting-started.md) for environment setup and certificate generation.

All backend settings are loaded from `.env` via `python-dotenv` (`settings.py`).

## Backend `.env`

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `BASE_URL` | Yes | `http://localhost:8001` | Public URL the backend is reachable at. Steam POSTs the OpenID callback here — must be publicly accessible (use ngrok in local dev). |
| `FRONTEND_URL` | Yes | `http://localhost:4200` | Angular app origin. Used for CORS and the post-login redirect. |
| `JWT_SECRET` | Yes | `change-this-secret` | Signs both access and refresh tokens. Use a strong random string in production. A warning is logged on startup if the default is detected. |
| `STEAM_API_KEY` | No | *(empty)* | Steam Web API key. Not required for login; reserved for future profile enrichment. Get one at https://steamcommunity.com/dev/apikey |

Example `.env`:
```dotenv
BASE_URL=https://xxxx.ngrok-free.app
FRONTEND_URL=https://localhost:4200
JWT_SECRET=your-random-64-char-secret-here
STEAM_API_KEY=
```

## Angular environment files

Two environment files control where HTTP calls go:

| File | Used when | `apiUrl` |
|------|-----------|----------|
| `environment.ts` | `ng serve` (dev web) | `/api` (proxied to `localhost:8001`) |
| `environment.prod.ts` | `ng build` / Android | `https://TBD` (dominio de producción por definir) |

The dev proxy target is set in `proxy.conf.json` (applies **only** to `ng serve`):

```json
{
  "/api": {
    "target": "https://localhost:8001",
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
