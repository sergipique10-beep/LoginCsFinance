# Steam OpenID Authentication — Angular + FastAPI Integration Guide

## How the flow works

Steam uses **OpenID 2.0**, which is a browser redirect protocol — not a direct API call. Angular cannot initiate it with `HttpClient` because the user's browser must physically navigate to Steam's login page, authenticate there, and be redirected back. No XHR or fetch call can replicate this.

The full redirect chain:

```
Angular app
  │
  │  1. window.location.href (or window.open)
  ▼
GET /auth/steam  ──────────────────────────────────────────────────────────
  │  Backend builds OpenID params and returns a 302 redirect
  ▼
https://steamcommunity.com/openid/login?openid.*=...
  │  User logs in on Steam; Steam issues its own 302 redirect
  ▼
GET /auth/steam/callback?openid.*=...
  │  Backend POSTs back to Steam to verify (check_authentication)
  │  Backend extracts the 64-bit SteamID from openid.claimed_id
  │  Backend issues a JWT (or session cookie)
  ▼
Angular app  ─── receives token, stores it, proceeds
```

Because Angular cannot intercept the Steam→backend redirect, you need one of the two strategies below to hand the resulting token back to your SPA.

---

## Option A — Full-page redirect (simplest)

Angular navigates the whole tab to the backend. After validation the backend redirects back to Angular with a JWT in the query string.

### 1. Angular — trigger login

```typescript
// src/app/services/auth.service.ts
import { Injectable } from '@angular/core';
import { environment } from '../../environments/environment';

@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly tokenKey = 'steam_token';

  /** Navigate the whole page to Steam login. */
  loginWithSteam(): void {
    window.location.href = `${environment.apiUrl}/auth/steam`;
  }

  /** Call this from the /auth/callback route component. */
  handleCallback(): void {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('token');
    if (token) {
      localStorage.setItem(this.tokenKey, token);
      // Clean the token out of the URL without a page reload
      window.history.replaceState({}, '', '/');
    }
  }

  getToken(): string | null {
    return localStorage.getItem(this.tokenKey);
  }

  logout(): void {
    localStorage.removeItem(this.tokenKey);
  }
}
```

```typescript
// src/app/pages/auth-callback/auth-callback.component.ts
import { Component, OnInit } from '@angular/core';
import { Router } from '@angular/router';
import { AuthService } from '../../services/auth.service';

@Component({
  standalone: true,
  selector: 'app-auth-callback',
  template: '<p>Logging you in...</p>',
})
export class AuthCallbackComponent implements OnInit {
  constructor(private auth: AuthService, private router: Router) {}

  ngOnInit(): void {
    this.auth.handleCallback();
    this.router.navigate(['/dashboard']);
  }
}
```

Register the route in your `app.routes.ts`:

```typescript
{ path: 'auth/callback', component: AuthCallbackComponent }
```

### 2. Backend — generate a JWT and redirect back

Install the JWT library:

```bash
pip install pyjwt
```

Add to `requirements.txt`:

```
pyjwt
```

Modify `main.py` — replace the final `return` in `steam_callback`:

```python
import jwt                          # add at top
from datetime import datetime, timezone, timedelta
from fastapi.responses import RedirectResponse
from settings import BASE_URL, STEAM_API_KEY, FRONTEND_URL, JWT_SECRET  # extend import

# ... existing validation code unchanged ...

    steam_id = match.group(1)

    # Optionally fetch profile (unchanged)
    profile = {}
    if STEAM_API_KEY:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                STEAM_API_URL,
                params={"key": STEAM_API_KEY, "steamids": steam_id},
            )
        players = resp.json().get("response", {}).get("players", [])
        profile = players[0] if players else {}

    # --- NEW: issue a JWT and redirect to the Angular app ---
    payload = {
        "sub": steam_id,
        "profile": profile,
        "exp": datetime.now(timezone.utc) + timedelta(hours=8),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    return RedirectResponse(url=f"{FRONTEND_URL}/auth/callback?token={token}")
```

Add to `settings.py`:

```python
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:4200")
JWT_SECRET   = os.getenv("JWT_SECRET", "change-me-in-production")
```

---

## Option B — Popup window

Angular keeps the main tab open and opens a small popup for the Steam login. After validation the backend serves a tiny HTML page that posts the token to the opener and closes itself.

### 1. Angular — open popup and listen for message

```typescript
// src/app/services/auth.service.ts
import { Injectable, NgZone, OnDestroy } from '@angular/core';
import { environment } from '../../environments/environment';

@Injectable({ providedIn: 'root' })
export class AuthService implements OnDestroy {
  private readonly tokenKey = 'steam_token';
  private messageHandler?: (e: MessageEvent) => void;

  constructor(private zone: NgZone) {}

  loginWithSteamPopup(): void {
    const popup = window.open(
      `${environment.apiUrl}/auth/steam`,
      'steam_login',
      'width=800,height=600,resizable=yes',
    );

    this.messageHandler = (event: MessageEvent) => {
      // Only accept messages from our own backend's relay page
      if (event.origin !== environment.apiUrl) return;
      const { token } = event.data as { token: string };
      if (token) {
        this.zone.run(() => {
          localStorage.setItem(this.tokenKey, token);
          popup?.close();
        });
      }
      window.removeEventListener('message', this.messageHandler!);
    };

    window.addEventListener('message', this.messageHandler);
  }

  getToken(): string | null {
    return localStorage.getItem(this.tokenKey);
  }

  logout(): void {
    localStorage.removeItem(this.tokenKey);
  }

  ngOnDestroy(): void {
    if (this.messageHandler) {
      window.removeEventListener('message', this.messageHandler);
    }
  }
}
```

### 2. Backend — serve a postMessage relay page

Replace the final return in `steam_callback` with:

```python
from fastapi.responses import HTMLResponse
from settings import BASE_URL, STEAM_API_KEY, FRONTEND_URL, JWT_SECRET

    # ... same JWT generation as Option A ...
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")

    html = f"""<!DOCTYPE html>
<html>
<body>
<script>
  window.opener.postMessage({{ token: "{token}" }}, "{FRONTEND_URL}");
  window.close();
</script>
</body>
</html>"""
    return HTMLResponse(content=html)
```

> Note: The `postMessage` origin must match `FRONTEND_URL` exactly, including protocol and port.

---

## CORS configuration

CORS is only needed if Angular makes **direct API calls** to the backend after authentication (e.g., fetching user data with the JWT). The Steam redirect chain itself does not trigger CORS.

Add to `main.py` (before any route definitions):

```python
from fastapi.middleware.cors import CORSMiddleware
from settings import FRONTEND_URL

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],   # e.g. "http://localhost:4200"
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
```

---

## Environment variables

### Backend `.env`

```dotenv
# Required — public URL the backend is reachable at (Steam will call this)
BASE_URL=http://localhost:8001

# Optional — fetches public Steam profile after login
# Get one at https://steamcommunity.com/dev/apikey
STEAM_API_KEY=

# Required for Option A or B
FRONTEND_URL=http://localhost:4200

# Required for Option A or B — use a long random string in production
JWT_SECRET=change-me-in-production
```

### Angular `src/environments/environment.ts`

```typescript
export const environment = {
  production: false,
  apiUrl: 'http://localhost:8001',
};
```

```typescript
// src/environments/environment.prod.ts
export const environment = {
  production: true,
  apiUrl: 'https://your-backend.example.com',
};
```

---

## Running locally

**1. Start the backend**

```bash
cd LoginCsFinance
python -m venv venv && source venv/Scripts/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8001 --reload
```

**2. Start Angular**

```bash
ng serve --port 4200
```

**3. (Important) Steam requires a publicly reachable callback URL.**

Steam's OpenID server will POST back to `BASE_URL/auth/steam/callback`. `localhost` works as long as Steam can reach it, but in practice you usually need a public tunnel:

```bash
# Install ngrok (https://ngrok.com), then:
ngrok http 8001
```

Update `.env`:

```dotenv
BASE_URL=https://xxxx-xxxx.ngrok-free.app
```

Restart uvicorn after changing `.env`. The Angular `apiUrl` can stay as `http://localhost:8001` for direct API calls — only `BASE_URL` needs to be the ngrok address.

---

## Quick reference

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check |
| `GET` | `/auth/steam` | Redirects browser to Steam OpenID login |
| `GET` | `/auth/steam/callback` | Steam posts back here; issues JWT; redirects to Angular |

### Environment variables

| Variable | Location | Required | Default |
|----------|----------|----------|---------|
| `BASE_URL` | `.env` | Yes | `http://localhost:8001` |
| `STEAM_API_KEY` | `.env` | No | *(empty — skips profile fetch)* |
| `FRONTEND_URL` | `.env` | Yes (Option A/B) | `http://localhost:4200` |
| `JWT_SECRET` | `.env` | Yes (Option A/B) | `change-me-in-production` |
| `apiUrl` | `environment.ts` | Yes | `http://localhost:8001` |

### Angular routes

| Path | Purpose |
|------|---------|
| `/auth/callback` | Landing page after Option A redirect; reads `?token=` param |
| Any protected route | Read token via `AuthService.getToken()` and attach to `Authorization: Bearer <token>` header |
