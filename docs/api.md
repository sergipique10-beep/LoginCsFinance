# API Reference

Base URL: `http://localhost:8000` (dev) — proxied via Angular as `/api`.

---

## Auth

### `GET /auth/steam`
Redirects the browser to Steam OpenID login. Nonce is embedded in `return_to`.

- Rate limited: 10 req / 60 s per IP
- Response: `302` to `https://steamcommunity.com/openid/login?...`

---

### `GET /auth/steam/callback`
Steam redirects here after login. Not called by Angular directly.

- Validates nonce, verifies with Steam, extracts SteamID
- On success: issues one-time code (TTL 30 s), redirects to `FRONTEND_URL/auth/callback?code=<code>`
- Errors: `400 Invalid or expired nonce`, `400 Tampered return_to URL`, `401 Steam authentication failed`, `400 Could not parse Steam ID`, `504 Steam validation timed out`, `502 Could not reach Steam servers`

---

### `POST /auth/token`
Exchange one-time code for tokens.

Request:
```json
{ "code": "string" }
```

Response `200`:
```json
{ "access_token": "eyJ..." }
```
Sets `Set-Cookie: refresh_token=...; HttpOnly; Secure; SameSite=Strict; Path=/`

Errors: `400 Invalid or expired code`

---

### `POST /auth/refresh`
Rotate refresh token. Reads `refresh_token` cookie automatically.

Must include `withCredentials: true` in Angular HTTP call.

Response `200`:
```json
{ "access_token": "eyJ..." }
```
Sets a new `refresh_token` cookie; previous `jti` is revoked.

Errors: `401` — cookie missing, expired, revoked, or wrong token type.

---

### `POST /auth/logout`
Revoke refresh token and clear cookie.

Must include `withCredentials: true`.

Response `200`:
```json
{ "ok": true }
```

---

## Protected routes

All require `Authorization: Bearer <access_token>`.

### `GET /me`
Response `200`:
```json
{ "steam_id": "76561198XXXXXXXXX" }
```
The `steam_id` value is read from `user["sub"]` — there is no separate `steam_id` claim in the JWT.

Errors: `401 Token expired`, `401 Invalid token`

---

### `GET /inventory`
Fetches the authenticated user's CS2 inventory via steamwebapi.com. The game is controlled by the `STEAM_GAME` setting (default `"cs2"`).

Response `200`: array of inventory item objects (structure defined by steamwebapi.com).

**Error responses:**

| Status | Detail | Cause |
|--------|--------|-------|
| `401` | `Token expired` / `Invalid token` | Missing or invalid Bearer token |
| `403` | `Inventory is private` | User's Steam inventory is set to private |
| `502` | `"Could not reach Steam: {exc}"` | `httpx.RequestError` — network failure contacting steamwebapi.com |
| `502` | `"Steam returned {status_code}"` | steamwebapi.com returned a non-200 status (e.g. 401, 403, 429) |
| `502` | `"Unexpected response format from Steam API"` | steamwebapi.com returned HTTP 200 but the body was not a JSON array (e.g. an error object) |

> **Known gap:** upstream errors from steamwebapi.com are not logged before the `raise HTTPException(status_code=502)` calls (`main.py` lines ~572, ~594, ~596). The actual status code and body returned by steamwebapi.com are silently discarded, making 502 responses opaque. See [troubleshooting.md](./troubleshooting.md#get-inventory-returns-502) for the diagnostic workaround.

---

## `GET /`
Health check.
```json
{ "status": "ok" }
```

---

## Security headers (all responses)

| Header | Value |
|--------|-------|
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
