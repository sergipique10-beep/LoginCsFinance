# Steam Auth — Angular + FastAPI Integration

## Estado actual del proyecto Angular

**Ruta del proyecto:** `C:\Users\Marc\Documents\CS-FINANCE\CS-FINANCE-ionic`

### Versiones instaladas (package.json real)

| Paquete | Versión |
|---------|---------|
| `@angular/core` | ^20.0.0 |
| `@ionic/angular` | ^8.0.0 |
| `@capacitor/core` | 8.3.1 |
| `@capacitor/android` | 8.3.1 |
| `@capacitor/app` | 8.1.0 |
| `@angular/fire` | ^20.0.1 |

**No instalado:** `@capacitor/browser` — necesario para el flujo Steam en Android.

### Dependencias a eliminar

`@angular/fire` está instalada pero **no se usa ni debe usarse** en este proyecto. La autenticación es exclusivamente Steam via el backend FastAPI.

```bash
cd C:\Users\Marc\Documents\CS-FINANCE\CS-FINANCE-ionic
npm uninstall @angular/fire
```

> `firebase` es una dependencia transitiva de `@angular/fire`, no una dependencia directa. Al desinstalar `@angular/fire`, `firebase` se elimina automáticamente al no quedar dependientes directos.

### Estado del login hoy vs. objetivo

| | Hoy | Objetivo |
|---|-----|----------|
| **Componente login** | `login.html` con un `<button class="login-button">Login with Steam</button>` sin lógica | Mismo HTML; añadir `(click)="auth.loginWithSteam()"` |
| **AuthService** | Clase vacía (`export class AuthService {}`) | Implementar con signals: `loginWithSteam()`, `exchangeCode()`, `refresh()`, `logout()` |
| **main.ts** | Importa y registra `provideFirebaseApp` y `provideAuth` con config Firebase via `@ngx-env/builder` | Eliminar todos los imports de `@angular/fire`; añadir `provideHttpClient(withInterceptors([authInterceptor]))` |
| **env.d.ts** | Declara 6 variables `NG_APP_FIREBASE_*` en `ImportMetaEnv` | Reemplazar por `NG_APP_API_URL` (o eliminar y usar `/api` hardcoded en dev) |
| **environment.ts** | Solo `{ production: false }` — sin `apiUrl` | Añadir `apiUrl: '/api'` |
| **environment.prod.ts** | Solo `{ production: true }` | Añadir `apiUrl: 'https://TBD'` (dominio de producción por definir) |

### Archivos que necesitan modificación

| Archivo | Qué cambia |
|---------|------------|
| `src/main.ts` | Eliminar `provideFirebaseApp`/`provideAuth`; añadir `provideHttpClient` con `authInterceptor` y `APP_INITIALIZER` para silent refresh |
| `src/env.d.ts` | Eliminar declaraciones Firebase; añadir `NG_APP_API_URL` si se usa `@ngx-env/builder` para la URL del API |
| `src/environments/environment.ts` | Añadir `apiUrl: '/api'` |
| `src/environments/environment.prod.ts` | Añadir `apiUrl: 'https://TBD'` (dominio de producción por definir) |
| `src/app/features/auth/services/auth.service.ts` | Implementar el servicio completo (ver skeleton más abajo) |
| `src/app/features/auth/pages/login.ts` | Inyectar `AuthService`; vincular botón a `loginWithSteam()` |
| `src/app/features/auth/pages/login.html` | Añadir `(click)="auth.loginWithSteam()"` al botón existente |
| `src/app/app.routes.ts` | Añadir ruta `/auth/callback` → `AuthCallbackComponent` |

### Archivos a crear

| Archivo | Qué es |
|---------|--------|
| `src/app/features/auth/pages/auth-callback.ts` | Componente que lee `?code=` de la URL y llama `auth.exchangeCode()` |
| `src/app/shared/interceptors/auth.interceptor.ts` | `HttpInterceptorFn` que añade el Bearer token y reintenta en 401 |
| `src/app/shared/guards/auth.guard.ts` | `CanActivateFn` que redirige a `/login` si no autenticado |
| `proxy.conf.json` | Proxy dev que reenvía `/api/*` a `https://localhost:8001` |

---

## Why the browser must navigate (not fetch)

Steam OpenID 2.0 is a browser-redirect protocol. Angular cannot initiate it with `HttpClient` — the user's browser must physically navigate to Steam. No XHR/fetch can replace this step.

---

## Auth flow overview

```
Angular                     Backend (FastAPI)              Steam
  │                               │                           │
  │  window.location.href         │                           │
  │  = /auth/steam ──────────────>│                           │
  │                               │──── 302 redirect ────────>│
  │                               │                   User logs in
  │                               │<─── callback ─────────────│
  │                               │  verify with Steam        │
  │                               │  issue one-time code      │
  │<── 302 /auth/callback?code=── │                           │
  │                               │                           │
  │  POST /auth/token { code } ──>│                           │
  │<── { access_token } + cookie ─│                           │
  │    (refresh token HttpOnly)   │                           │
```

---

## Endpoints

### `GET /auth/steam`
Redirects the browser to Steam OpenID. No body, no params.

### `GET /auth/steam/callback`
Steam returns here. Backend:
1. Validates nonce (CSRF protection)
2. POSTs to Steam for `check_authentication`
3. Extracts 17-digit SteamID from `openid.claimed_id`
4. Issues a **one-time auth code** (TTL 30 s, stored in `_code_store`)
5. Redirects to `FRONTEND_URL/auth/callback?code=<code>`

### `POST /auth/token`
Exchange the one-time code for tokens.

Request body:
```json
{ "code": "<one-time code>" }
```

Response `200 OK`:
```json
{ "access_token": "<JWT>" }
```
Sets `Set-Cookie: refresh_token=<JWT>; HttpOnly; Secure; SameSite=Strict; Path=/`

Errors: `400 Invalid or expired code`

### `POST /auth/refresh`
Rotate the refresh token. Reads the `refresh_token` cookie automatically (no body needed).

Response `200 OK`:
```json
{ "access_token": "<new JWT>" }
```
Sets a new `refresh_token` cookie; old `jti` is revoked in `_refresh_store`.

Errors: `401` if cookie missing, expired, revoked, or wrong type.

### `POST /auth/logout`
Revoke the refresh token and clear the cookie. No body needed.

Response `200 OK`:
```json
{ "ok": true }
```

### `GET /me`
Protected route. Requires `Authorization: Bearer <access_token>`.

Response `200 OK`:
```json
{ "steam_id": "76561198XXXXXXXXX" }
```

Errors: `401 Token expired` / `401 Invalid token`

---

## Token design

| Token | Type | TTL | Transport | Storage |
|-------|------|-----|-----------|---------|
| Access token | JWT HS256 | 30 min | `Authorization: Bearer` header | Angular in-memory only |
| Refresh token | JWT HS256 | 7 days | `refresh_token` cookie | `_refresh_store` (in-memory; TODO Redis) |

**Access token claims:** `sub`, `steam_id`, `iat`, `exp`, `aud: "cs-finance"`, `type: "access"`

**Refresh token claims:** `sub`, `jti` (UUID, for revocation), `iat`, `exp`, `aud: "cs-finance"`, `type: "refresh"`

**Refresh cookie flags:** `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`

---

## Angular integration

### Dev proxy (`proxy.conf.json`)
Avoids CORS in development by proxying all `/api/*` calls to the backend:

```json
{
  "/api": {
    "target": "http://localhost:8001",
    "secure": false,
    "pathRewrite": { "^/api": "" }
  }
}
```

Run with: `ng serve --proxy-config proxy.conf.json`

All Angular HTTP calls use `/api/auth/...` — never hardcode `localhost:8001`.

### AuthService (skeleton)

Uses Angular signals — no RxJS state management needed.

```typescript
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly http = inject(HttpClient);

  // in-memory only — never localStorage
  private readonly _accessToken = signal<string | null>(null);

  readonly isAuthenticated = computed(() => {
    const token = this._accessToken();
    if (!token) return false;
    try {
      const { exp } = JSON.parse(atob(token.split('.')[1]));
      return exp * 1000 > Date.now();
    } catch { return false; }
  });

  loginWithSteam(): void {
    window.location.href = '/api/auth/steam';
  }

  async exchangeCode(code: string): Promise<void> {
    const res = await firstValueFrom(
      this.http.post<{ access_token: string }>('/api/auth/token', { code })
    );
    this._accessToken.set(res.access_token);
  }

  async refresh(): Promise<boolean> {
    try {
      const res = await firstValueFrom(
        this.http.post<{ access_token: string }>(
          '/api/auth/refresh', {}, { withCredentials: true }
        )
      );
      this._accessToken.set(res.access_token);
      return true;
    } catch { return false; }
  }

  async logout(): Promise<void> {
    await firstValueFrom(
      this.http.post('/api/auth/logout', {}, { withCredentials: true })
    );
    this._accessToken.set(null);
  }

  getToken(): string | null { return this._accessToken(); }
  setToken(token: string): void { this._accessToken.set(token); }
  clearToken(): void { this._accessToken.set(null); }
}
```

### `/auth/callback` component

```typescript
@Component({ standalone: true, template: '<p>Logging you in...</p>' })
export class AuthCallbackComponent implements OnInit {
  constructor(private auth: AuthService, private router: Router,
              private route: ActivatedRoute) {}

  async ngOnInit(): Promise<void> {
    const code = this.route.snapshot.queryParamMap.get('code');
    if (code) {
      await this.auth.exchangeCode(code);
      // Remove ?code= from URL before navigating
      await this.router.navigate(['/dashboard'], { replaceUrl: true });
    }
  }
}
```

### APP_INITIALIZER (silent session restore)

Uses `inject()` inside `useFactory` — no `deps` array needed in Angular 14+.

```typescript
// app.config.ts
export const appConfig: ApplicationConfig = {
  providers: [
    provideRouter(routes),
    provideHttpClient(withInterceptors([authInterceptor])),
    {
      provide: APP_INITIALIZER,
      useFactory: () => {
        const auth = inject(AuthService);
        return () => auth.refresh();
      },
      multi: true,
    },
  ],
};
```

On every app load, `POST /api/auth/refresh` is called. If the refresh cookie is valid, the access token is restored silently. If not (no cookie / expired), the call returns `false` and the user stays unauthenticated.

### HTTP Interceptor

Functional interceptor (Angular 15+, standard in Angular 20). Registered via `provideHttpClient(withInterceptors([authInterceptor]))` in `app.config.ts`.

```typescript
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService);
  const token = auth.getToken();
  const authed = token
    ? req.clone({ setHeaders: { Authorization: `Bearer ${token}` } })
    : req;

  return next(authed).pipe(
    catchError(err => {
      if (err.status === 401) {
        return from(auth.refresh()).pipe(
          switchMap(ok => ok
            ? next(req.clone({ setHeaders: { Authorization: `Bearer ${auth.getToken()}` } }))
            : throwError(() => err)
          )
        );
      }
      return throwError(() => err);
    })
  );
};
```

### AuthGuard

Functional guard — no class, no `@Injectable`.

```typescript
export const authGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  return auth.isAuthenticated() ? true : router.createUrlTree(['/login']);
};
```

---

## Ionic / Capacitor (Android)

### Steam login in a native WebView

Inside a Capacitor WebView, `window.location.href` redirects within the WebView itself. This breaks the Steam OpenID flow because the return URL never reaches Angular as a route change — it tries to resolve inside the embedded browser.

**Fix:** use `@capacitor/browser` to open Steam in the system browser, then return via a deep link (App URL Scheme).

```typescript
import { Browser } from '@capacitor/browser';
import { Capacitor } from '@capacitor/core';

loginWithSteam(): void {
  if (Capacitor.isNativePlatform()) {
    Browser.open({ url: `${environment.apiUrl}/auth/steam` });
  } else {
    window.location.href = '/api/auth/steam';  // dev web with proxy
  }
}
```

The backend `FRONTEND_URL` must be set to the app's URL scheme in production native builds (e.g. `myapp://auth/callback`) so the post-login redirect lands back in the app. In dev web it stays as `https://localhost:4200`.

### Cookies HttpOnly in Android WebView

HttpOnly cookies work in Capacitor's WebView (Chrome WebView on Android), but cross-origin requests require `SameSite=None; Secure` on the cookie if the WebView origin and the API are on different origins. This differs from dev web, where `SameSite=Strict` works fine because the proxy makes them same-origin.

For `/auth/refresh` and `/auth/logout`, always include `withCredentials: true` so the WebView sends the cookie.

### Environment files

```typescript
// environment.ts (dev web — ng serve con proxy)
export const environment = { production: false, apiUrl: '/api' };

// environment.prod.ts (Android nativo — URL de producción por definir)
export const environment = { production: true, apiUrl: 'https://TBD' };
```

The proxy (`proxy.conf.json`) is only active during `ng serve`. Native builds bypass it entirely and hit `environment.apiUrl` directly.

---

## Angular routes

| Path | Component | Notes |
|------|-----------|-------|
| `/auth/callback` | `AuthCallbackComponent` | Reads `?code=`, exchanges for tokens |
| `/login` | `LoginComponent` | Calls `auth.loginWithSteam()` |
| `/dashboard` | `DashboardComponent` | Protected by `AuthGuard` |

---

## Environment variables (backend)

See [configuration](./configuration.md) for the full list.

Key variables for this flow:

| Variable | Required | Default |
|----------|----------|---------|
| `BASE_URL` | Yes | `http://localhost:8001` |
| `FRONTEND_URL` | Yes | `http://localhost:4200` |
| `JWT_SECRET` | Yes | `change-this-secret` |

---

## Security notes

- The one-time code expires in 30 s and is consumed on first use — replay is not possible.
- Access tokens are never written to `localStorage` or `sessionStorage`.
- The refresh token travels only via `HttpOnly` cookie — invisible to JavaScript.
- CSRF on the refresh/logout endpoints is mitigated by `SameSite=Strict`.
- Nonces (TTL 300 s) prevent replay of the Steam OpenID callback.
- `_refresh_store` is in-memory and is lost on restart. Migrate to Redis before horizontal scaling.
