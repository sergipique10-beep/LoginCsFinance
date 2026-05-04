# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Create and activate virtual environment (Windows)
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Start the server (auto-reload on file changes)
uvicorn main:app --host 127.0.0.1 --port 8001 --reload

# Health check
curl http://localhost:8001/
```

There are no test or lint commands configured. The project has no test suite.

## Architecture

This is a **stateless Steam OpenID 2.0 authentication gateway** — a FastAPI microservice that bridges an Angular SPA (port 4200) and Steam login. The entire app logic lives in `main.py` (68 lines).

**Authentication flow:**
1. Angular redirects browser to `GET /auth/steam`
2. Backend builds OpenID 2.0 params and redirects to `https://steamcommunity.com/openid/login`
3. After Steam login, Steam redirects to `GET /auth/steam/callback?openid.*=...`
4. Backend validates the response by POST-ing back to Steam (`check_authentication`)
5. Backend extracts the 64-bit Steam ID from `openid.claimed_id` via regex
6. Backend issues a 24-hour JWT and redirects to `FRONTEND_URL/auth/callback?token={JWT}`
7. Angular stores the token in localStorage

**Key files:**
- `main.py` — all three endpoints (`/`, `/auth/steam`, `/auth/steam/callback`)
- `settings.py` — loads config from `.env` via `python-dotenv`
- `.env` — local environment variables (not committed)
- `docs/steam-auth-angular.md` — full walkthrough of the OpenID flow and Angular integration

## Environment Variables

| Variable | Purpose | Notes |
|----------|---------|-------|
| `BASE_URL` | Public URL of this backend | Steam must be able to reach the callback URL; use ngrok locally if needed |
| `FRONTEND_URL` | Angular app URL | Backend redirects here after issuing JWT |
| `STEAM_API_KEY` | Steam Web API key | Currently unused in code; reserved for profile fetching |
| `JWT_SECRET` | Signing secret for JWTs | Must be kept secret; rotate in production |

Steam's OpenID requires `BASE_URL` to be reachable from the internet for the callback. Use [ngrok](https://ngrok.com/) (`ngrok http 8001`) for local development against the real Steam API.
