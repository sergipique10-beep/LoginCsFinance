# Manual Broadcast Push Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Poder enviar un push notification con título y texto libres a todos los dispositivos registrados, disparado manualmente desde GitHub Actions.

**Architecture:** Un nuevo endpoint `POST /internal/broadcast` en el router de notifications, protegido por un token compartido en el header `X-Broadcast-Token`, que llama al `send_broadcast()` que ya existe. `send_broadcast()` pasa a devolver contadores (`sent`/`failed`/`pruned`) para poder diagnosticar por qué una push no llega. Un workflow de GitHub Actions con `workflow_dispatch` hace el POST.

**Tech Stack:** FastAPI, Pydantic v2, firebase-admin, Supabase (supabase-py), pytest + `fastapi.testclient.TestClient`, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-07-13-manual-broadcast-design.md`

## Global Constraints

- El endpoint **no toca la tabla `notified_news`** — un anuncio manual no se deduplica.
- El `data` del mensaje FCM va **vacío** (`{}`). No se añade `url` ni deep-link (fuera de alcance).
- Token propio `BROADCAST_TOKEN` — **no** se reutiliza `NEWS_TICK_TOKEN`.
- Comparación del token con `secrets.compare_digest` (nunca `==`).
- Límites de longitud: `title` 1-100 caracteres, `body` 1-240 caracteres.
- Patrón de settings del repo: el router hace `from settings import X` (import por nombre), y los tests hacen `monkeypatch.setattr(notifications_router, "X", "...")`. Respetarlo — si el router leyera `settings.X` los tests existentes dejarían de funcionar.
- Los tests se ejecutan desde `LoginCsfinance/` con el venv activado (`venv\Scripts\activate`).

---

### Task 1: `send_broadcast()` devuelve contadores

Hoy `send_broadcast()` devuelve `None`. Sin contadores no se puede distinguir "la tabla `device_tokens` está vacía" de "se envió pero FCM lo rechazó" — que es exactamente la información que hace falta cuando la notificación no aparece en el teléfono.

**Files:**
- Modify: `notifications/service.py:41-63` (función `send_broadcast`)
- Test: `tests/test_notifications_service.py`

**Interfaces:**
- Consumes: nada de tareas anteriores.
- Produces: `async def send_broadcast(title: str, body: str, data: dict[str, str]) -> dict[str, int]` — devuelve `{"sent": int, "failed": int, "pruned": int}`. La Task 2 depende de esta firma.

**Nota de implementación:** los contadores se derivan de `response.responses` (la lista de resultados individuales), **no** de `response.success_count` / `response.failure_count`. Motivo: los tests existentes mockean `send_each_for_multicast` con un objeto falso que solo expone `.responses`, y usar los atributos agregados los rompería.

- [ ] **Step 1: Escribir los tests que fallan**

Añadir al final de `tests/test_notifications_service.py`:

```python
def test_send_broadcast_returns_counters(monkeypatch):
    from firebase_admin import messaging

    monkeypatch.setattr(
        notifications_service.repo, "list_device_tokens", AsyncMock(return_value=["tok-a", "tok-b", "tok-c"])
    )
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", AsyncMock())
    monkeypatch.setattr(notifications_service, "_get_firebase_app", lambda: object())

    class FakeResult:
        def __init__(self, success, exception=None):
            self.success = success
            self.exception = exception

    fake_batch_response = type(
        "FakeBatch",
        (),
        {
            "responses": [
                FakeResult(True),
                FakeResult(False, messaging.UnregisteredError("gone")),
                FakeResult(False, ValueError("boom")),
            ]
        },
    )()
    monkeypatch.setattr(messaging, "send_each_for_multicast", lambda message, app: fake_batch_response)

    result = asyncio.run(notifications_service.send_broadcast("Title", "Body", {}))

    assert result == {"sent": 1, "failed": 2, "pruned": 1}


def test_send_broadcast_returns_zeros_with_no_tokens(monkeypatch):
    monkeypatch.setattr(notifications_service.repo, "list_device_tokens", AsyncMock(return_value=[]))

    result = asyncio.run(notifications_service.send_broadcast("Title", "Body", {}))

    assert result == {"sent": 0, "failed": 0, "pruned": 0}
```

Fíjate en el tercer `FakeResult`: un fallo que **no** es `UnregisteredError` (un `ValueError`). Cuenta como `failed` pero **no** se poda — solo los tokens `UnregisteredError` son tokens muertos que hay que borrar. Eso es lo que separa `failed` de `pruned`.

- [ ] **Step 2: Ejecutar los tests para verificar que fallan**

Run: `pytest tests/test_notifications_service.py -v -k "counters or zeros"`
Expected: FAIL — `assert None == {'sent': 1, ...}` (la función todavía devuelve `None`).

- [ ] **Step 3: Implementar**

En `notifications/service.py`, reemplazar la función `send_broadcast` entera por:

```python
async def send_broadcast(title: str, body: str, data: dict[str, str]) -> dict[str, int]:
    tokens = await repo.list_device_tokens()
    if not tokens:
        return {"sent": 0, "failed": 0, "pruned": 0}

    def _do():
        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            data=data,
            tokens=tokens,
        )
        return messaging.send_each_for_multicast(message, app=_get_firebase_app())

    response = await asyncio.to_thread(_do)

    sent = sum(1 for r in response.responses if r.success)
    failed = len(response.responses) - sent

    invalid = [
        tokens[i]
        for i, r in enumerate(response.responses)
        if not r.success and isinstance(r.exception, messaging.UnregisteredError)
    ]
    if invalid:
        logger.info("[notifications] pruning %d unregistered token(s)", len(invalid))
        await repo.delete_device_tokens(invalid)

    return {"sent": sent, "failed": failed, "pruned": len(invalid)}
```

- [ ] **Step 4: Ejecutar toda la suite de notifications**

Run: `pytest tests/test_notifications_service.py -v`
Expected: PASS — los 6 tests (los 4 que ya existían siguen pasando: `check_and_notify_new_news` ignora el valor de retorno, así que no le afecta).

- [ ] **Step 5: Commit**

```bash
git add notifications/service.py tests/test_notifications_service.py
git commit -m "feat: send_broadcast devuelve contadores sent/failed/pruned"
```

---

### Task 2: `POST /internal/broadcast`

**Files:**
- Modify: `settings.py` (añadir `BROADCAST_TOKEN`)
- Modify: `main.py:9-14` (import) y `main.py:53-57` (warning de arranque)
- Modify: `notifications/router.py` (nuevo body model + endpoint)
- Test: `tests/test_notifications_router.py`

**Interfaces:**
- Consumes: `service.send_broadcast(title, body, data) -> dict[str, int]` de la Task 1.
- Produces: `POST /internal/broadcast`, header `X-Broadcast-Token`, body `{"title": str, "body": str}`, respuesta `{"sent": N, "failed": M, "pruned": K}`. La Task 3 (workflow) depende de este contrato.

- [ ] **Step 1: Escribir los tests que fallan**

Añadir al final de `tests/test_notifications_router.py`:

```python
def test_broadcast_requires_token_header(client, monkeypatch):
    monkeypatch.setattr(notifications_router, "BROADCAST_TOKEN", "secret123")

    resp = client.post("/internal/broadcast", json={"title": "Hola", "body": "Mundo"})

    assert resp.status_code == 401


def test_broadcast_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr(notifications_router, "BROADCAST_TOKEN", "secret123")

    resp = client.post(
        "/internal/broadcast",
        json={"title": "Hola", "body": "Mundo"},
        headers={"X-Broadcast-Token": "wrong"},
    )

    assert resp.status_code == 401


def test_broadcast_sends_with_valid_token(client, monkeypatch):
    monkeypatch.setattr(notifications_router, "BROADCAST_TOKEN", "secret123")
    mock_send = AsyncMock(return_value={"sent": 3, "failed": 0, "pruned": 0})
    monkeypatch.setattr(notifications_service, "send_broadcast", mock_send)

    resp = client.post(
        "/internal/broadcast",
        json={"title": "Nueva version", "body": "Ya disponible la v2"},
        headers={"X-Broadcast-Token": "secret123"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"sent": 3, "failed": 0, "pruned": 0}
    mock_send.assert_awaited_once_with(title="Nueva version", body="Ya disponible la v2", data={})


def test_broadcast_rejects_empty_title(client, monkeypatch):
    monkeypatch.setattr(notifications_router, "BROADCAST_TOKEN", "secret123")

    resp = client.post(
        "/internal/broadcast",
        json={"title": "", "body": "Mundo"},
        headers={"X-Broadcast-Token": "secret123"},
    )

    assert resp.status_code == 422


def test_broadcast_rejects_too_long_body(client, monkeypatch):
    monkeypatch.setattr(notifications_router, "BROADCAST_TOKEN", "secret123")

    resp = client.post(
        "/internal/broadcast",
        json={"title": "Hola", "body": "x" * 241},
        headers={"X-Broadcast-Token": "secret123"},
    )

    assert resp.status_code == 422
```

Nota: `test_broadcast_rejects_empty_title` verifica que la validación (422) ocurre aunque el token sea válido — el `data={}` en `assert_awaited_once_with` verifica el Global Constraint de que no se manda `newsId` ni `url`.

- [ ] **Step 2: Ejecutar los tests para verificar que fallan**

Run: `pytest tests/test_notifications_router.py -v -k broadcast`
Expected: FAIL — los 5 tests dan 404 (la ruta no existe) en vez de 401/200/422.

- [ ] **Step 3: Añadir `BROADCAST_TOKEN` a `settings.py`**

Después de la línea de `NEWS_TICK_TOKEN` (línea 29):

```python
# Token que protege POST /internal/broadcast (anuncio manual, workflow_dispatch).
BROADCAST_TOKEN = os.getenv("BROADCAST_TOKEN", "")
```

- [ ] **Step 4: Implementar el endpoint**

En `notifications/router.py`:

Cambiar el import de settings (línea 9) a:

```python
from settings import NEWS_TICK_TOKEN, BROADCAST_TOKEN
```

Añadir el body model junto a los otros (después de `DeleteTokenBody`):

```python
class BroadcastBody(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=240)
```

y ampliar el import de pydantic en la línea 6:

```python
from pydantic import BaseModel, Field
```

Añadir el endpoint al final del fichero:

```python
@router.post("/internal/broadcast", summary="Envía una push manual a todos los dispositivos (anuncio)")
async def broadcast(body: BroadcastBody, x_broadcast_token: str | None = Header(default=None)):
    if not BROADCAST_TOKEN or not x_broadcast_token or not secrets.compare_digest(x_broadcast_token, BROADCAST_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing broadcast token")

    return await service.send_broadcast(title=body.title, body=body.body, data={})
```

`secrets`, `Header` y `HTTPException` ya están importados en el fichero (los usa `news_tick`).

- [ ] **Step 5: Ejecutar los tests**

Run: `pytest tests/test_notifications_router.py -v`
Expected: PASS — los 10 tests (5 nuevos + los 5 que ya existían).

- [ ] **Step 6: Añadir el warning de arranque en `main.py`**

Ampliar el import de settings (líneas 9-14) añadiendo `BROADCAST_TOKEN` a la lista:

```python
from settings import (
    ALLOWED_CORS_ORIGINS, JWT_SECRET, STEAM_API_KEY,
    SUPABASE_URL, SUPABASE_SERVICE_KEY, CAP_TICK_TOKEN,
    REVIEW_USER, REVIEW_PASSWORD, REVIEW_STEAM_ID,
    FIREBASE_SERVICE_ACCOUNT_JSON, NEWS_TICK_TOKEN, BROADCAST_TOKEN,
)
```

Y añadir el warning en el `lifespan`, justo después del bloque de `NEWS_TICK_TOKEN`:

```python
    if not BROADCAST_TOKEN:
        logger.warning(
            "BROADCAST_TOKEN no está configurada — "
            "el anuncio manual (POST /internal/broadcast) no funcionará"
        )
```

- [ ] **Step 7: Ejecutar la suite completa**

Run: `pytest -v`
Expected: PASS — toda la suite, incluidos `test_inventory_refresh.py` y los de notifications.

- [ ] **Step 8: Commit**

```bash
git add settings.py main.py notifications/router.py tests/test_notifications_router.py
git commit -m "feat: POST /internal/broadcast para anuncios push manuales"
```

---

### Task 3: Workflow de GitHub Actions + documentación

**Files:**
- Create: `.github/workflows/broadcast.yml`
- Modify: `.env.example` (añadir `BROADCAST_TOKEN`)
- Modify: `CLAUDE.md` (tabla de Endpoints + tabla de variables de entorno)

**Interfaces:**
- Consumes: `POST /internal/broadcast` con header `X-Broadcast-Token` de la Task 2.
- Produces: nada (es la capa final).

- [ ] **Step 1: Crear el workflow**

Crear `.github/workflows/broadcast.yml`:

```yaml
name: broadcast

# Anuncio manual: envía una push notification con título y texto libres a todos
# los dispositivos registrados en device_tokens. Solo se dispara a mano
# (workflow_dispatch) — nunca en schedule.

on:
  workflow_dispatch:
    inputs:
      title:
        description: "Título de la notificación (máx. 100 caracteres)"
        required: true
        type: string
      body:
        description: "Texto de la notificación (máx. 240 caracteres)"
        required: true
        type: string

jobs:
  send:
    runs-on: ubuntu-latest
    steps:
      - name: POST /internal/broadcast
        run: |
          payload=$(jq -n --arg title "$TITLE" --arg body "$BODY" '{title: $title, body: $body}')
          curl -sS --fail-with-body -X POST "$BASE_URL/internal/broadcast" \
            -H "X-Broadcast-Token: $BROADCAST_TOKEN" \
            -H "Content-Type: application/json" \
            -d "$payload"
        env:
          BASE_URL: ${{ secrets.BACKEND_BASE_URL }}
          BROADCAST_TOKEN: ${{ secrets.BROADCAST_TOKEN }}
          TITLE: ${{ inputs.title }}
          BODY: ${{ inputs.body }}
```

Tres decisiones deliberadas aquí, no las "simplifiques":

1. **El JSON se construye con `jq -n --arg`, no interpolando strings.** Un título con comillas o saltos de línea rompería un JSON hecho a mano, o permitiría inyectar campos extra en el body.
2. **Los inputs pasan por `env:`, no se interpolan dentro del `run:`.** Interpolar `${{ inputs.title }}` directamente en el script de shell sería inyección de comandos — el input es texto que escribe el operador.
3. **Sin `--max-time`.** Render free duerme por inactividad; el primer request puede tardar 30-50 s en despertar el servicio. Un timeout agresivo daría un falso error.

`--fail-with-body` hace que un 401/500 marque el job en rojo (en vez de pasar en silencio) e imprime el cuerpo del error. `jq` viene preinstalado en `ubuntu-latest`.

- [ ] **Step 2: Añadir `BROADCAST_TOKEN` a `.env.example`**

Junto a `NEWS_TICK_TOKEN`:

```
BROADCAST_TOKEN=
```

- [ ] **Step 3: Actualizar `CLAUDE.md`**

En la tabla de **Endpoints**, justo debajo de la fila de `/internal/news-tick`:

```markdown
| POST | `/internal/broadcast` | `X-Broadcast-Token` | Anuncio manual (`workflow_dispatch` de GitHub Actions). Envía un push con `{title, body}` libres a todos los `device_tokens`. `data` vacío → al tocar, la app abre Home. No deduplica: no toca `notified_news`. Devuelve `{sent, failed, pruned}`. |
```

En la tabla de **Environment variables**, debajo de `NEWS_TICK_TOKEN`:

```markdown
| `BROADCAST_TOKEN` | *(empty)* | Shared secret protegiendo `POST /internal/broadcast`. Debe coincidir con el GitHub Secret del mismo nombre. Startup warns if missing. |
```

En la sección **Module structure**, actualizar la línea del router de notifications:

```
    router.py         # APIRouter: /notifications/register-token, /notifications/delete-token,
                      #            /internal/news-tick, /internal/broadcast
```

- [ ] **Step 4: Verificar que el YAML es válido**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/broadcast.yml')); print('ok')"`
Expected: `ok`

- [ ] **Step 5: Ejecutar la suite completa una última vez**

Run: `pytest -v`
Expected: PASS — nada de esta tarea toca código Python, así que debe seguir todo en verde.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/broadcast.yml .env.example CLAUDE.md
git commit -m "feat: workflow de anuncio manual + docs de BROADCAST_TOKEN"
```

---

## Pasos manuales (fuera del código — los hace el usuario)

Estos no los puede hacer un agente; son la parte que convierte el código en una notificación real en el teléfono.

1. **Generar el token:**
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. **Render** (dashboard del servicio `cs-finance-api` → Environment): añadir `BROADCAST_TOKEN` con ese valor. El servicio se redespliega solo.
3. **GitHub** (Settings → Secrets and variables → Actions): añadir el secret `BROADCAST_TOKEN` con **el mismo valor**. (`BACKEND_BASE_URL` ya existe — lo usan `cap-tick`, `market-tick` y `news-tick`.)
4. **Mergear la rama a `master` y pushear** — un `workflow_dispatch` solo aparece en la pestaña Actions si el fichero del workflow está en la rama por defecto.
5. **Lanzarlo:** pestaña Actions → "broadcast" → Run workflow → escribir título y texto → Run workflow.

## Verificación end-to-end

Con el móvil a mano y **la app en segundo plano o cerrada** — con la app en primer plano FCM entrega el mensaje al listener en vez de pintar la notificación en la bandeja del sistema, y `NotificationsService` no muestra nada en ese caso, así que parecería que no ha llegado cuando sí llegó.

Lanzar el workflow y leer el log del run:

- **`{"sent": 1, ...}`** (o más) + notificación en el teléfono → funciona, listo.
- **`{"sent": 0, "failed": 0, "pruned": 0}`** → la tabla `device_tokens` está vacía. El fallo está en el **registro** del token desde la app, no en el envío: el móvil nunca llamó a `POST /notifications/register-token` con éxito. Mirar los logs de Render para ver si esa llamada llegó, y comprobar que la app tiene permiso de notificaciones concedido.
- **`{"sent": 0, "failed": N}`** con `pruned: N` → los tokens guardados están muertos (app reinstalada). Ya se han podado solos; reabrir la app en el móvil para que registre uno nuevo y relanzar el workflow.
- **401 en el log** → el `BROADCAST_TOKEN` de GitHub y el de Render no coinciden.
