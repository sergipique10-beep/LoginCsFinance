# API Reference

Base URL: `http://localhost:8001` (dev) — proxied via Angular as `/api`.

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
