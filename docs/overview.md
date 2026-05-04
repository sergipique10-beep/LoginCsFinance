# LoginCsFinance — Overview

FastAPI backend that authenticates users via **Steam OpenID 2.0** and issues JWTs for a companion Angular SPA (CS-Finance).

## Architecture

```
Angular SPA (localhost:4200)
  └── dev proxy /api/* → localhost:8001
        │
        ├── GET  /auth/steam          (redirect to Steam)
        ├── GET  /auth/steam/callback (Steam returns here)
        ├── POST /auth/token          (exchange one-time code → tokens)
        ├── POST /auth/refresh        (rotate refresh token)
        ├── POST /auth/logout         (revoke refresh token)
        └── GET  /me                  (protected — requires Bearer token)

FastAPI (main.py)
  ├── run_dev.py        — local dev launcher (uvicorn + SSL via certs/)
  ├── settings.py       — env var loading
  ├── SecurityHeadersMiddleware
  ├── CORSMiddleware    — origin locked to FRONTEND_URL
  ├── rate limiter      — 10 req/60 s per IP (in-memory)
  ├── nonce store       — CSRF protection for OpenID (in-memory)
  └── refresh store     — revocation list for refresh tokens (in-memory)
```

## Token model

Two-token architecture: short-lived access token (30 min, in-memory in Angular) + long-lived refresh token (7 days, `HttpOnly` cookie). See [steam-auth-angular.md](./steam-auth-angular.md) for the full flow.

## Key constraints

- **Single-worker only** — nonces, rate-limit counters, and refresh token store are all in-memory. Migrate to Redis before running multiple uvicorn workers or restarting without losing sessions.
- **Steam callback must be publicly reachable** — Steam's servers POST the OpenID response to `BASE_URL/auth/steam/callback`. Use ngrok or similar in local dev.

## Docs index

- [getting-started.md](./getting-started.md) — Local setup: dependencies, HTTPS certs, and running the stack
- [configuration.md](./configuration.md) — All environment variables
- [api.md](./api.md) — Endpoint reference
- [steam-auth-angular.md](./steam-auth-angular.md) — Full auth flow + Angular integration patterns
