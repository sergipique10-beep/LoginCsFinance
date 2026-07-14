# Manual broadcast push — design

**Fecha:** 2026-07-13
**Estado:** aprobado, pendiente de plan

## Problema

El sistema de push notifications ya está montado end-to-end: la app registra su token FCM
(`NotificationsService` → `POST /notifications/register-token`), los tokens viven en la tabla
`device_tokens` de Supabase, y `notifications/service.py:send_broadcast()` envía un multicast vía
firebase-admin.

Pero **no hay forma de enviar una push arbitraria**. El único llamador de `send_broadcast()` es
`check_and_notify_new_news()`, que es idempotente: deduplica por `gid` contra la tabla
`notified_news`, así que una noticia ya notificada nunca se reenvía. Eso lo hace inservible como
prueba, y no cubre el caso de querer anunciar algo que no es una noticia de Steam (una nueva versión
de la app, un mantenimiento).

## Objetivo

Una capacidad permanente de **anuncio manual**: enviar un push con título y texto libres a todos los
dispositivos registrados, cuando el operador quiera. Sirve además como prueba de humo de la cadena
completa (Supabase → firebase-admin → FCM → teléfono).

## Alcance

Solo `title` + `body`. El `data` del mensaje va vacío (`{}`) y al tocar la notificación la app abre
`/tabs/home`, que es el comportamiento que el listener de `NotificationsService` ya tiene hoy
(ignora el `data` por completo).

**Fuera de alcance (YAGNI):** deep-link por `url` en el payload. Añadirlo obligaría a tocar el
listener del frontend y por tanto a recompilar y reinstalar el APK, y es puramente aditivo — se
puede añadir después sin rehacer nada de esto.

## Diseño

### 1. `send_broadcast()` devuelve contadores

`notifications/service.py:send_broadcast()` pasa de devolver `None` a devolver
`{"sent": N, "failed": M, "pruned": K}`, derivado de la `BatchResponse` de
`messaging.send_each_for_multicast`.

Motivo: sin esto, al probar desde el móvil no se puede distinguir "la tabla `device_tokens` está
vacía" de "se envió pero FCM lo rechazó". Esa distinción es la información de diagnóstico principal
cuando la notificación no aparece en el teléfono.

Con la tabla vacía (early return) devuelve `{"sent": 0, "failed": 0, "pruned": 0}`.

`check_and_notify_new_news()` no cambia: simplemente ignora el valor de retorno.

### 2. `POST /internal/broadcast`

Nuevo endpoint en `notifications/router.py`, siguiendo el patrón de `news_tick`.

| Aspecto | Decisión |
|---------|----------|
| Auth | Header `X-Broadcast-Token`, comparado con `secrets.compare_digest` contra la env var `BROADCAST_TOKEN`. Ausente o inválido → 401. |
| Body | `BroadcastBody` (Pydantic): `title: str` (`min_length=1, max_length=100`), `body: str` (`min_length=1, max_length=240`). |
| Lógica | Llama a `service.send_broadcast(title, body, data={})` y devuelve su resultado. |
| Respuesta | `{"sent": N, "failed": M, "pruned": K}` |

**Token propio, no se reutiliza `NEWS_TICK_TOKEN`.** Son dos capacidades distintas —una la dispara un
cron automático con contenido derivado de Steam, la otra manda texto libre a todos los usuarios— y la
filtración de una no debe conceder la otra.

**No toca `notified_news`.** Un anuncio manual no es una noticia de Steam: no debe deduplicarse ni
contaminar la tabla de dedup del cron. Enviar el mismo texto dos veces es legítimo.

El pruning de tokens `UnregisteredError` que ya hace `send_broadcast()` sigue funcionando igual.

### 3. `.github/workflows/broadcast.yml`

Trigger **solo `workflow_dispatch`** (nunca `schedule`), con inputs `title` y `body`, ambos required.

Un único step con `curl -sS --fail-with-body -X POST "$BASE_URL/internal/broadcast"`, header
`X-Broadcast-Token`, y el JSON construido con `jq -n --arg title ... --arg body ...` — **no** por
interpolación de strings: un título con comillas o saltos de línea rompería el JSON o permitiría
inyectar campos.

`--fail-with-body` hace que un 401/500 marque el job en rojo en vez de pasar en silencio, e imprime
el cuerpo del error. La respuesta (`{"sent": N, ...}`) queda en el log del run.

Sin `--max-time`: Render free duerme por inactividad y el primer request puede tardar 30-50 s en
despertar el servicio. Un timeout agresivo daría un falso error.

Secretos: `BACKEND_BASE_URL` (ya existe, lo usan los otros workflows) y `BROADCAST_TOKEN` (nuevo).

### 4. Configuración

- `settings.py`: `BROADCAST_TOKEN = os.getenv("BROADCAST_TOKEN", "")`, incluido en el warning de
  arranque del `lifespan` que ya avisa de secretos ausentes.
- Render: env var `BROADCAST_TOKEN`.
- GitHub Secrets: `BROADCAST_TOKEN`, mismo valor. Generado con
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `CLAUDE.md`: fila del endpoint en la tabla de Endpoints, fila de `BROADCAST_TOKEN` en la de
  variables de entorno.

## Tests

Siguiendo `tests/test_notifications_service.py`:

- Endpoint: 401 sin header, 401 con token incorrecto, 200 con token válido (con `send_broadcast`
  mockeado, verificando que recibe `data={}`), 422 con `title` o `body` vacíos.
- `send_broadcast`: devuelve los contadores correctos con `messaging.send_each_for_multicast`
  mockeado; devuelve ceros con la tabla vacía; sigue podando los tokens `UnregisteredError`.

## Verificación end-to-end

Tras desplegar y configurar los secretos: lanzar el workflow desde la pestaña Actions y comprobar
(a) que el log del run dice `"sent"` ≥ 1, y (b) que la notificación llega al teléfono.

`"sent": 0` significa que `device_tokens` está vacía → el fallo está en el registro del token desde
la app, no en el envío.

**La app debe estar en segundo plano o cerrada durante la prueba.** Con la app en primer plano, FCM
entrega el mensaje al listener en vez de pintar una notificación en la bandeja del sistema, y
`NotificationsService` no muestra nada en ese caso — parecería que no ha llegado cuando sí llegó.
