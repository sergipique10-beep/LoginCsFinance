# Push notifications — CS2 news (design)

> Este diseño abarca dos repos: `LoginCsfinance` (backend) y `CS-FINANCE-ionic` (frontend). Se documenta aquí porque el backend es la pieza nueva de infraestructura (Firebase Admin, tablas, cron); el plan de implementación se ejecutará como dos tareas coordinadas, una por repo.

## Contexto

CS-FINANCE no tiene push notifications hoy. El objetivo es notificar a los usuarios cuando aparece una noticia nueva de CS2 (`GET /news/cs2`), en Android e iOS.

## Alcance (Fase 1)

**Incluido:**
- Push de noticias CS2 nuevas, broadcast a todos los dispositivos registrados (no personalizado por usuario).
- Registro de token FCM automático tras login (nuevo o restaurado), solo en plataformas nativas (Android/iOS vía Capacitor).
- Cron horario (GitHub Actions) que detecta noticias nuevas y dispara el envío.
- Código listo para iOS; el build/firma real en Xcode queda pendiente de acceso a un Mac.

**Explícitamente fuera de alcance (fases futuras):**
- Alertas de subida de precio sobre el inventario del usuario. Bloqueado por el límite de 5 req/día del plan gratuito de steamwebapi.com — revisar periódicamente el inventario de cada usuario agotaría ese cupo. Se diseñará en una fase 2 separada, limitado a como mucho 1 check/día y reutilizando datos ya cacheados (23h) en vez de polling activo.
- Push en navegador/web (Web Push + Service Worker). No se pidió y el plugin elegido no lo cubre.
- Toggle de preferencias en Profile — el permiso se pide automáticamente tras login; no hay opt-out explícito en esta fase.
- Personalización de contenido por usuario — todos los dispositivos reciben la misma noticia.

## Transporte: Firebase Cloud Messaging

- Se crea un proyecto Firebase (plan Spark, gratis) usado únicamente para FCM — no Auth, no Firestore, no repetir el patrón de AngularFire que ya se retiró del frontend.
- **Frontend**: plugin `@capacitor-firebase/messaging` (no el `@capacitor/push-notifications` oficial). Este plugin usa el SDK de Firebase también en iOS, de modo que **ambas plataformas exponen un token FCM unificado** — con el plugin oficial, iOS entrega un token APNs crudo y el backend tendría que hablar con Apple directamente, duplicando la lógica de envío. Trade-off aceptado: dependemos de un plugin comunitario (capawesome-team) en vez de solo paquetes core de Capacitor.
- **Backend**: `firebase-admin` (Python), inicializado una vez en el lifespan de FastAPI con una service account key.

### Requisitos nativos (documentar, no bloquean el desarrollo)
- Android: `google-services.json` en `android/app/`.
- iOS: `GoogleService-Info.plist` en el proyecto Xcode + clave APNs subida a la consola Firebase. Pendiente hasta tener acceso a Mac.

## Backend (LoginCsfinance)

### Tablas Supabase nuevas

```sql
device_tokens (
  token       text primary key,
  platform    text not null,        -- 'android' | 'ios'
  created_at  timestamptz not null default now()
)

notified_news (
  gid          text primary key,    -- id de la noticia (item["gid"] en Steam News API)
  notified_at  timestamptz not null default now()
)
```

No se guarda `steam_id` en `device_tokens`: el contenido es broadcast (misma noticia para todos), así que no hay personalización ni necesidad de desregistrar tokens en logout.

### Módulo `notifications/` (mismo patrón que `auth/`)

- `service.py`:
  - `register_token(token, platform)` — upsert en `device_tokens` por `token`.
  - `send_broadcast(title, body, data)` — `firebase_admin.messaging.send_each_for_multicast` contra todos los tokens de `device_tokens`. Tras el envío, borra de la tabla los tokens que FCM reporte como `NotRegistered` / `InvalidArgument`.
  - `check_and_notify_new_news()` — reutiliza la lógica existente de `steam/routes/news.py` para traer las últimas N noticias, filtra las que no estén en `notified_news`, inserta el dedup y llama a `send_broadcast` por cada noticia nueva (o agrupadas en un único push si aparece más de una a la vez).
- `router.py`:
  - `POST /notifications/register-token` — Bearer auth (consistencia con el resto de la API). Body `{ token: str, platform: 'android' | 'ios' }`.
  - `POST /internal/news-tick` — header `X-News-Tick-Token` (mismo patrón que `X-Cap-Token` en `/internal/cap-tick`), comparado con `secrets.compare_digest`. Llama a `check_and_notify_new_news()`.

### Cron

Nuevo workflow `.github/workflows/news-tick.yml`, calcado de `cap-tick.yml` pero con horario desfasado (`15 * * * *` en vez de `5 * * * *`, para no coincidir):

```yaml
name: news-tick
on:
  schedule:
    - cron: "15 * * * *"
  workflow_dispatch: {}
jobs:
  tick:
    runs-on: ubuntu-latest
    steps:
      - name: POST /internal/news-tick
        run: |
          curl -fsS -X POST "$BASE_URL/internal/news-tick" -H "X-News-Tick-Token: $NEWS_TICK_TOKEN"
        env:
          BASE_URL: ${{ secrets.BACKEND_BASE_URL }}
          NEWS_TICK_TOKEN: ${{ secrets.NEWS_TICK_TOKEN }}
```

### Nuevas variables de entorno

| Variable | Notas |
|----------|-------|
| `FIREBASE_SERVICE_ACCOUNT_JSON` | JSON completo de la service account de Firebase, como string (igual que otros secretos del proyecto). Startup warning si falta. |
| `NEWS_TICK_TOKEN` | Secreto compartido con GitHub Actions para `/internal/news-tick`. Startup warning si falta. |

## Frontend (CS-FINANCE-ionic)

- Nueva dependencia: `@capacitor-firebase/messaging`.
- `android/app/google-services.json` añadido al proyecto Android (gitignored si contiene claves sensibles — seguir el patrón de `.env`).
- Nuevo `NotificationsService` (inyectado con `inject()`, sin `@Input()`/NgModules, siguiendo las convenciones del proyecto):
  - `registerForPush()`: solo actúa si `Capacitor.isNativePlatform()`. Pide permiso nativo, obtiene el token FCM, hace `POST /notifications/register-token` vía `SteamApiService` con `{ token, platform: Capacitor.getPlatform() }`.
  - Listener de `notificationActionPerformed` (tap en la notificación): navega a `/tabs/home` con `Router`.
- **Disparo del registro**: se llama `registerForPush()` en dos puntos —
  1. `AuthCallback`, justo después de intercambiar el código por el access token con éxito (login nuevo).
  2. `APP_INITIALIZER` (junto a `auth.refresh()`), cuando la sesión ya existe al abrir la app (sesión restaurada).

## Manejo de errores y edge cases

- Fallo al registrar el token (red caída, backend dormido en Render free) → falla en silencio, no bloquea el login ni el arranque de la app.
- Notificación con la app cerrada → el SO la muestra igualmente; al tocarla, Capacitor abre/foreground la app y dispara `notificationActionPerformed`.
- Tokens inválidos/expirados devueltos por FCM tras un envío → se eliminan automáticamente de `device_tokens`.
- El cron es idempotente vía `notified_news`: si `news-tick` corre dos veces (reintento), una noticia ya notificada no se reenvía.

## Testing

- Backend: sin suite de tests configurada en el proyecto (consistente con el resto). Verificación manual: `curl -X POST .../internal/news-tick -H "X-News-Tick-Token: ..."` y comprobar en logs/Supabase que se detectan noticias nuevas y se llama a FCM.
- Frontend: prueba manual en un dispositivo Android físico o emulador con Google Play Services (FCM no funciona en emuladores sin Play Store). iOS queda pendiente de validación hasta tener acceso a un Mac con Xcode.
