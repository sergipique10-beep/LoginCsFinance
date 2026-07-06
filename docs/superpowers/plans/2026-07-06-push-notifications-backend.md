# Push Notifications (Backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `notifications` module to the FastAPI backend that lets the app register FCM device tokens and broadcasts a push notification whenever a new CS2 news article appears, via an hourly external cron.

**Architecture:** New `notifications/` package (mirrors the existing `auth/` package structure: `repo.py` for Supabase persistence, `service.py` for business logic, `router.py` for the two HTTP endpoints). Reuses the existing cached Supabase client from `steam/cap_history_repo.py` rather than creating a second one. Firebase Admin SDK (`firebase-admin`) sends the actual push via FCM, working for both Android and iOS tokens since the frontend uses `@capacitor-firebase/messaging` (unified FCM token on both platforms — see the design spec).

**Tech Stack:** FastAPI, Supabase (Postgres), `firebase-admin` (Python), pytest + `fastapi.testclient.TestClient` (existing test setup in `tests/`), GitHup Actions cron (same pattern as the existing `cap-tick` workflow).

**Companion plan:** `CS-FINANCE-ionic/docs/superpowers/plans/2026-07-06-push-notifications-frontend.md` (separate repo, separate plan — this backend plan produces working, curl-testable endpoints on its own).

**Related spec:** `docs/superpowers/specs/2026-07-06-push-notifications-design.md`

## Global Constraints

- Phase 1 only sends **broadcast** CS2-news push — no per-user personalization, no inventory price alerts (explicitly out of scope, see spec).
- `device_tokens` table has **no** `steam_id` column — tokens are not tied to an account.
- Follow existing absolute-import style at the top-level (`from settings import X`, `from steam.mappers import Y`), matching `main.py` / `auth/router.py`.
- Follow existing module-level import style for testability: `from . import repo` / `from . import service` (not `from .repo import fn`), so `monkeypatch.setattr(module, "fn", ...)` works exactly like the existing `tests/test_inventory_refresh.py` pattern.
- New Supabase tables get RLS enabled with **no policies** (service_role key bypasses RLS) — same as `market_cap_history`.
- No steamwebapi.com calls anywhere in this feature (Steam News API is a separate, unlimited Steam endpoint — already used unauthenticated by `/news/cs2`).
- requirements.txt is UTF-16LE-encoded (an artifact of `pip freeze > requirements.txt` in Windows PowerShell) — regenerate it the same way to keep the encoding consistent, don't hand-edit with a UTF-8 editor.

---

### Task 1: Supabase tables — `device_tokens` and `notified_news`

**Files:**
- None in this repo (Supabase-side migration only, applied via the Supabase MCP tool)

**Interfaces:**
- Produces: two tables other tasks read/write via `steam.cap_history_repo.get_supabase()`.

- [ ] **Step 1: Find the `cs-finance` Supabase project id**

Call the Supabase MCP tool `list_projects` and locate the project whose name matches `cs-finance` (the same project already used for `market_cap_history`, per `CLAUDE.md`). Note its `id` for the next step.

- [ ] **Step 2: Apply the migration**

Call the Supabase MCP tool `apply_migration` against that project id with:

```sql
create table public.device_tokens (
  token       text primary key,
  platform    text not null check (platform in ('android', 'ios')),
  created_at  timestamptz not null default now()
);

alter table public.device_tokens enable row level security;

create table public.notified_news (
  gid          text primary key,
  notified_at  timestamptz not null default now()
);

alter table public.notified_news enable row level security;
```

Name the migration `create_device_tokens_and_notified_news`.

- [ ] **Step 3: Verify**

Call the Supabase MCP tool `list_tables` for the same project and confirm `device_tokens` and `notified_news` both appear under `public` with the columns above.

---

### Task 2: Settings + Supabase repo layer

**Files:**
- Modify: `settings.py`
- Create: `notifications/__init__.py` (empty, marks the package)
- Create: `notifications/repo.py`

**Interfaces:**
- Consumes: `steam.cap_history_repo.get_supabase() -> Client` (existing, unchanged).
- Produces (for Task 3):
  - `async def register_device_token(token: str, platform: str) -> None`
  - `async def list_device_tokens() -> list[str]`
  - `async def delete_device_tokens(tokens: list[str]) -> None`
  - `async def filter_new_news_gids(gids: list[str]) -> list[str]`
  - `async def mark_news_notified(gids: list[str]) -> None`

- [ ] **Step 1: Add the new env vars to `settings.py`**

Add after the existing `CAP_TICK_TOKEN` line (`settings.py:18`):

```python
# Firebase Admin SDK: envía push notifications (FCM) a Android e iOS.
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")

# Token que protege POST /internal/news-tick (cron externo de GitHub Actions).
NEWS_TICK_TOKEN = os.getenv("NEWS_TICK_TOKEN", "")
```

- [ ] **Step 2: Create the `notifications` package**

Create `notifications/__init__.py` (empty file).

- [ ] **Step 3: Write `notifications/repo.py`**

```python
"""
Persistencia de push notifications en Supabase: tokens de dispositivo (FCM)
y noticias CS2 ya notificadas (dedup para el cron de news-tick).

Reutiliza el cliente Supabase cacheado de steam/cap_history_repo.py — mismo
proyecto Supabase, no hace falta un segundo cliente.
"""
import asyncio

from steam.cap_history_repo import get_supabase

_DEVICE_TOKENS_TABLE = "device_tokens"
_NOTIFIED_NEWS_TABLE = "notified_news"


async def register_device_token(token: str, platform: str) -> None:
    def _do() -> None:
        get_supabase().table(_DEVICE_TOKENS_TABLE).upsert(
            {"token": token, "platform": platform}, on_conflict="token"
        ).execute()

    await asyncio.to_thread(_do)


async def list_device_tokens() -> list[str]:
    def _do() -> list[str]:
        resp = get_supabase().table(_DEVICE_TOKENS_TABLE).select("token").execute()
        return [row["token"] for row in (resp.data or [])]

    return await asyncio.to_thread(_do)


async def delete_device_tokens(tokens: list[str]) -> None:
    if not tokens:
        return

    def _do() -> None:
        get_supabase().table(_DEVICE_TOKENS_TABLE).delete().in_("token", tokens).execute()

    await asyncio.to_thread(_do)


async def filter_new_news_gids(gids: list[str]) -> list[str]:
    """Returns the subset of gids NOT already present in notified_news."""
    if not gids:
        return []

    def _do() -> list[str]:
        resp = (
            get_supabase()
            .table(_NOTIFIED_NEWS_TABLE)
            .select("gid")
            .in_("gid", gids)
            .execute()
        )
        already_notified = {row["gid"] for row in (resp.data or [])}
        return [g for g in gids if g not in already_notified]

    return await asyncio.to_thread(_do)


async def mark_news_notified(gids: list[str]) -> None:
    if not gids:
        return

    def _do() -> None:
        rows = [{"gid": g} for g in gids]
        get_supabase().table(_NOTIFIED_NEWS_TABLE).upsert(rows, on_conflict="gid").execute()

    await asyncio.to_thread(_do)
```

No dedicated tests for this file — `steam/cap_history_repo.py` (the identical pattern this mirrors) has zero direct test coverage in this repo either; it's exercised indirectly through the service-layer tests in Task 3, matching that existing convention.

- [ ] **Step 4: Commit**

```bash
git add settings.py notifications/__init__.py notifications/repo.py
git commit -m "feat(notifications): add Supabase repo layer for device tokens and notified news"
```

---

### Task 3: `firebase-admin` dependency + `notifications/service.py`

**Files:**
- Modify: `requirements.txt`
- Create: `notifications/service.py`
- Create: `tests/test_notifications_service.py`

**Interfaces:**
- Consumes: `notifications.repo` (Task 2), `steam.mappers._clean_news_content(raw: str, max_chars: int = 220) -> str` (existing).
- Produces (for Task 4):
  - `async def register_token(token: str, platform: str) -> None`
  - `async def send_broadcast(title: str, body: str, data: dict[str, str]) -> None`
  - `async def check_and_notify_new_news(http_client: httpx.AsyncClient) -> dict` — returns `{"notified": <int>}`

- [ ] **Step 1: Install `firebase-admin` and regenerate `requirements.txt`**

Run in PowerShell (inside the activated venv, `venv\Scripts\activate`):

```powershell
pip install firebase-admin
pip freeze > requirements.txt
```

Using PowerShell's `>` redirect (not bash) keeps the file's existing UTF-16LE encoding consistent with the rest of the file.

- [ ] **Step 2: Write the failing tests for `check_and_notify_new_news`**

Create `tests/test_notifications_service.py`:

```python
import asyncio
from unittest.mock import AsyncMock

from notifications import service as notifications_service

RAW_NEWS = [
    {"gid": "111", "title": "Nuevo update CS2", "contents": "Contenido de prueba", "url": "https://example.com/1"},
    {"gid": "222", "title": "Otro parche", "contents": "Mas contenido", "url": "https://example.com/2"},
]


def test_check_and_notify_skips_already_notified(monkeypatch):
    monkeypatch.setattr(notifications_service, "_fetch_raw_news", AsyncMock(return_value=RAW_NEWS))
    monkeypatch.setattr(notifications_service.repo, "filter_new_news_gids", AsyncMock(return_value=["222"]))
    mark_mock = AsyncMock()
    monkeypatch.setattr(notifications_service.repo, "mark_news_notified", mark_mock)
    send_mock = AsyncMock()
    monkeypatch.setattr(notifications_service, "send_broadcast", send_mock)

    result = asyncio.run(notifications_service.check_and_notify_new_news(http_client=None))

    assert result == {"notified": 1}
    send_mock.assert_awaited_once_with(
        title="Otro parche",
        body="Mas contenido",
        data={"newsId": "222", "url": "https://example.com/2"},
    )
    mark_mock.assert_awaited_once_with(["222"])


def test_check_and_notify_returns_zero_when_nothing_new(monkeypatch):
    monkeypatch.setattr(notifications_service, "_fetch_raw_news", AsyncMock(return_value=RAW_NEWS))
    monkeypatch.setattr(notifications_service.repo, "filter_new_news_gids", AsyncMock(return_value=[]))
    send_mock = AsyncMock()
    monkeypatch.setattr(notifications_service, "send_broadcast", send_mock)

    result = asyncio.run(notifications_service.check_and_notify_new_news(http_client=None))

    assert result == {"notified": 0}
    send_mock.assert_not_awaited()


def test_send_broadcast_prunes_unregistered_tokens(monkeypatch):
    from firebase_admin import messaging

    monkeypatch.setattr(notifications_service.repo, "list_device_tokens", AsyncMock(return_value=["tok-a", "tok-b"]))
    delete_mock = AsyncMock()
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", delete_mock)
    monkeypatch.setattr(notifications_service, "_get_firebase_app", lambda: object())

    class FakeResult:
        def __init__(self, success, exception=None):
            self.success = success
            self.exception = exception

    fake_batch_response = type(
        "FakeBatch", (), {"responses": [FakeResult(True), FakeResult(False, messaging.UnregisteredError("gone"))]}
    )()
    monkeypatch.setattr(messaging, "send_each_for_multicast", lambda message, app: fake_batch_response)

    asyncio.run(notifications_service.send_broadcast("Title", "Body", {"k": "v"}))

    delete_mock.assert_awaited_once_with(["tok-b"])


def test_send_broadcast_noop_with_no_tokens(monkeypatch):
    monkeypatch.setattr(notifications_service.repo, "list_device_tokens", AsyncMock(return_value=[]))
    delete_mock = AsyncMock()
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", delete_mock)

    asyncio.run(notifications_service.send_broadcast("Title", "Body", {}))

    delete_mock.assert_not_awaited()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_notifications_service.py -v`
Expected: `ModuleNotFoundError: No module named 'notifications.service'` (or `AttributeError`) — the module doesn't exist yet.

- [ ] **Step 4: Write `notifications/service.py`**

```python
"""
Business logic for push notifications: registering FCM tokens, broadcasting
via Firebase Admin SDK, and detecting new CS2 news to notify about.
"""
import asyncio
import json
import logging

import httpx
import firebase_admin
from firebase_admin import credentials, messaging

from settings import FIREBASE_SERVICE_ACCOUNT_JSON
from steam.mappers import _clean_news_content
from . import repo

logger = logging.getLogger("uvicorn.error")

STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
_NEWS_TICK_COUNT = 10

_firebase_app: firebase_admin.App | None = None


def _get_firebase_app() -> firebase_admin.App:
    global _firebase_app
    if _firebase_app is None:
        if not FIREBASE_SERVICE_ACCOUNT_JSON:
            raise RuntimeError(
                "FIREBASE_SERVICE_ACCOUNT_JSON no configurada — no se pueden enviar push"
            )
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
        _firebase_app = firebase_admin.initialize_app(cred, name="cs-finance-notifications")
    return _firebase_app


async def register_token(token: str, platform: str) -> None:
    await repo.register_device_token(token, platform)


async def send_broadcast(title: str, body: str, data: dict[str, str]) -> None:
    tokens = await repo.list_device_tokens()
    if not tokens:
        return

    def _do():
        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            data=data,
            tokens=tokens,
        )
        return messaging.send_each_for_multicast(message, app=_get_firebase_app())

    response = await asyncio.to_thread(_do)

    invalid = [
        tokens[i]
        for i, r in enumerate(response.responses)
        if not r.success and isinstance(r.exception, messaging.UnregisteredError)
    ]
    if invalid:
        logger.info("[notifications] pruning %d unregistered token(s)", len(invalid))
        await repo.delete_device_tokens(invalid)


async def _fetch_raw_news(http_client: httpx.AsyncClient, count: int = _NEWS_TICK_COUNT) -> list[dict]:
    resp = await http_client.get(
        STEAM_NEWS_URL,
        params={"appid": 730, "count": count, "format": "json"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json().get("appnews", {}).get("newsitems", [])


async def check_and_notify_new_news(http_client: httpx.AsyncClient) -> dict:
    newsitems = await _fetch_raw_news(http_client)
    gids = [str(item["gid"]) for item in newsitems if item.get("gid")]

    new_gids = await repo.filter_new_news_gids(gids)
    if not new_gids:
        return {"notified": 0}

    new_gids_set = set(new_gids)
    new_items = [item for item in newsitems if str(item.get("gid", "")) in new_gids_set]

    for item in new_items:
        title = item.get("title", "CS2 News")[:100]
        body = _clean_news_content(item.get("contents", ""), max_chars=140) or title
        await send_broadcast(
            title=title,
            body=body,
            data={"newsId": str(item["gid"]), "url": item.get("url", "")},
        )

    await repo.mark_news_notified(new_gids)
    return {"notified": len(new_items)}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_notifications_service.py -v`
Expected: `4 passed`

- [ ] **Step 6: Commit**

```bash
git add requirements.txt notifications/service.py tests/test_notifications_service.py
git commit -m "feat(notifications): add firebase-admin broadcast service and news-tick logic"
```

---

### Task 4: Router endpoints + `main.py` wiring

**Files:**
- Create: `notifications/router.py`
- Create: `tests/test_notifications_router.py`
- Modify: `main.py`

**Interfaces:**
- Consumes: `notifications.service.register_token`, `notifications.service.check_and_notify_new_news` (Task 3), `auth.service.require_jwt` (existing).
- Produces: `POST /notifications/register-token`, `POST /internal/news-tick` HTTP endpoints.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_notifications_router.py`:

```python
from unittest.mock import AsyncMock

from notifications import router as notifications_router
from notifications import service as notifications_service


def test_register_token_persists_via_service(client, monkeypatch):
    mock_register = AsyncMock()
    monkeypatch.setattr(notifications_service, "register_token", mock_register)

    resp = client.post("/notifications/register-token", json={"token": "abc123", "platform": "android"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    mock_register.assert_awaited_once_with("abc123", "android")


def test_register_token_rejects_invalid_platform(client):
    resp = client.post("/notifications/register-token", json={"token": "abc123", "platform": "windows"})
    assert resp.status_code == 422


def test_news_tick_requires_token_header(client, monkeypatch):
    monkeypatch.setattr(notifications_router, "NEWS_TICK_TOKEN", "secret123")

    resp = client.post("/internal/news-tick")

    assert resp.status_code == 401


def test_news_tick_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr(notifications_router, "NEWS_TICK_TOKEN", "secret123")

    resp = client.post("/internal/news-tick", headers={"X-News-Tick-Token": "wrong"})

    assert resp.status_code == 401


def test_news_tick_calls_service_with_valid_token(client, monkeypatch):
    monkeypatch.setattr(notifications_router, "NEWS_TICK_TOKEN", "secret123")
    mock_check = AsyncMock(return_value={"notified": 2})
    monkeypatch.setattr(notifications_service, "check_and_notify_new_news", mock_check)

    resp = client.post("/internal/news-tick", headers={"X-News-Tick-Token": "secret123"})

    assert resp.status_code == 200
    assert resp.json() == {"notified": 2}
    mock_check.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_notifications_router.py -v`
Expected: `ModuleNotFoundError: No module named 'notifications.router'`

- [ ] **Step 3: Write `notifications/router.py`**

```python
import secrets
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from auth.service import require_jwt
from settings import NEWS_TICK_TOKEN
from . import service

logger = logging.getLogger("uvicorn.error")

router = APIRouter()


class RegisterTokenBody(BaseModel):
    token: str
    platform: Literal["android", "ios"]


@router.post("/notifications/register-token", summary="Registra un token FCM para push notifications")
async def register_token(body: RegisterTokenBody, _payload: dict = Depends(require_jwt)):
    await service.register_token(body.token, body.platform)
    return {"status": "ok"}


@router.post("/internal/news-tick", summary="Detecta noticias CS2 nuevas y envía push (cron interno)")
async def news_tick(request: Request, x_news_tick_token: str | None = Header(default=None)):
    if not NEWS_TICK_TOKEN or not x_news_tick_token or not secrets.compare_digest(x_news_tick_token, NEWS_TICK_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing news-tick token")

    return await service.check_and_notify_new_news(request.app.state.http_client)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_notifications_router.py -v`
Expected: `5 passed`

- [ ] **Step 5: Wire into `main.py`**

Modify the imports at the top of `main.py` (`main.py:9-16`):

```python
from settings import (
    ALLOWED_CORS_ORIGINS, JWT_SECRET, STEAM_API_KEY,
    SUPABASE_URL, SUPABASE_SERVICE_KEY, CAP_TICK_TOKEN,
    FIREBASE_SERVICE_ACCOUNT_JSON, NEWS_TICK_TOKEN,
)
from middleware import SecurityHeadersMiddleware
from auth.router import router as auth_router
from steam.routes import router as steam_router
from steam.services import _fetch_static_images
from notifications.router import router as notifications_router
```

Add startup warnings inside `lifespan` (`main.py:38-42`, right after the existing Supabase/cap-tick warning block):

```python
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY and CAP_TICK_TOKEN):
        logger.warning(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY / CAP_TICK_TOKEN incompletas — "
            "el histórico del índice de precio (cap-history) no funcionará"
        )
    if not FIREBASE_SERVICE_ACCOUNT_JSON:
        logger.warning(
            "FIREBASE_SERVICE_ACCOUNT_JSON no está configurada — "
            "las push notifications no funcionarán"
        )
    if not NEWS_TICK_TOKEN:
        logger.warning(
            "NEWS_TICK_TOKEN no está configurada — "
            "el cron de noticias (news-tick) no funcionará"
        )
```

Register the router next to the others (`main.py:61-62`):

```python
app.include_router(auth_router)
app.include_router(steam_router)
app.include_router(notifications_router)
```

- [ ] **Step 6: Run the full test suite**

Run: `pytest -v`
Expected: all tests pass, including the pre-existing `tests/test_inventory_refresh.py`.

- [ ] **Step 7: Commit**

```bash
git add notifications/router.py tests/test_notifications_router.py main.py
git commit -m "feat(notifications): add register-token and news-tick endpoints"
```

---

### Task 5: GitHub Actions cron + docs

**Files:**
- Create: `.github/workflows/news-tick.yml`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `POST /internal/news-tick` (Task 4).

- [ ] **Step 1: Create the workflow**

Create `.github/workflows/news-tick.yml`:

```yaml
name: news-tick

# Cron externo que detecta noticias CS2 nuevas y dispara push notifications
# (broadcast a todos los dispositivos registrados). Desfasado 10 minutos del
# cron de cap-tick para no coincidir.

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

- [ ] **Step 2: Add the GitHub Actions secret**

In the repo's GitHub settings (Settings → Secrets and variables → Actions), add `NEWS_TICK_TOKEN` with the same value configured in the backend's `.env` / Render environment variables (`BACKEND_BASE_URL` already exists from the `cap-tick` workflow).

- [ ] **Step 3: Update `CLAUDE.md`**

Add a new row to the **Endpoints** table (after the `/internal/cap-tick` row):

```markdown
| POST | `/notifications/register-token` | Bearer | Registra un token FCM (`{ token, platform }`) para recibir push notifications. |
| POST | `/internal/news-tick` | `X-News-Tick-Token` | Cron horario (GitHub Actions). Detecta noticias CS2 nuevas y envía push broadcast vía FCM. Idempotente (dedup por `gid` en `notified_news`). |
```

Add two rows to the **Environment variables** table:

```markdown
| `FIREBASE_SERVICE_ACCOUNT_JSON` | *(empty)* | JSON completo de la service account de Firebase (Firebase Admin SDK), como string. Startup warns if missing. |
| `NEWS_TICK_TOKEN` | *(empty)* | Shared secret protecting `POST /internal/news-tick`. Must match the GitHub Actions secret. Startup warns if missing. |
```

Add a line to the **Module structure** section, next to the `steam/` entry:

```markdown
  notifications/
    repo.py           # Supabase data layer: device_tokens, notified_news (reuses steam/cap_history_repo's client)
    service.py        # register_token, send_broadcast (firebase-admin), check_and_notify_new_news
    router.py         # APIRouter: /notifications/register-token, /internal/news-tick
```

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/news-tick.yml CLAUDE.md
git commit -m "docs+ci: add news-tick cron workflow and update CLAUDE.md"
```

---

## Manual end-to-end verification (after deploying `FIREBASE_SERVICE_ACCOUNT_JSON` and `NEWS_TICK_TOKEN` to Render)

```bash
# Register a fake token (needs a valid Bearer access token from a real login,
# or /auth/dev-token when DEBUG=true)
curl -X POST https://cs-finance-api.onrender.com/notifications/register-token \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"token": "test-token-123", "platform": "android"}'
# Expected: {"status":"ok"}

# Trigger the news-tick manually
curl -X POST https://cs-finance-api.onrender.com/internal/news-tick \
  -H "X-News-Tick-Token: <NEWS_TICK_TOKEN>"
# Expected: {"notified": N} where N is 0 on a repeat run (idempotent)
```
