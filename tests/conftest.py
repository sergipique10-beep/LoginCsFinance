from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

import main
from main import app
from auth.service import require_jwt
from stores import (
    _auth_codes, _inventory_cache, _inventory_refresh_cooldown,
    _nonces, _rate_store,
)
from auth import refresh_repo
from steam import inventory_snapshot_repo

STEAM_ID = "test_steam_id"

# PERF-14: la tabla inventory_snapshots, en memoria. steam_id → (items, captured_at).
SNAPSHOT_DB: dict[str, tuple[list, str]] = {}


@pytest.fixture(autouse=True)
def _fake_snapshot_repo(monkeypatch):
    """Ningún test escribe en el Supabase real por el snapshot de inventario."""
    SNAPSHOT_DB.clear()

    async def _save(steam_id, items):
        SNAPSHOT_DB[steam_id] = (items, "2026-10-03T10:00:00+00:00")

    async def _load(steam_id):
        return SNAPSHOT_DB.get(steam_id)

    async def _delete(steam_id):
        SNAPSHOT_DB.pop(steam_id, None)

    monkeypatch.setattr(inventory_snapshot_repo, "save", _save)
    monkeypatch.setattr(inventory_snapshot_repo, "load", _load)
    monkeypatch.setattr(inventory_snapshot_repo, "delete_for_user", _delete)
    yield
    SNAPSHOT_DB.clear()

# SEC-11: la tabla refresh_tokens, en memoria. jti → (steam_id, expires_at).
REFRESH_DB: dict[str, tuple[str, datetime]] = {}


async def _fake_save(jti, steam_id, expires_at):
    REFRESH_DB[jti] = (steam_id, expires_at)


async def _fake_consume(jti, steam_id):
    """Misma semántica que el DELETE ... RETURNING real: vigente, del mismo usuario, un solo uso."""
    fila = REFRESH_DB.get(jti)
    if not fila or fila[0] != steam_id or fila[1] <= datetime.now(timezone.utc):
        return False
    del REFRESH_DB[jti]
    return True


async def _fake_revoke(jti):
    REFRESH_DB.pop(jti, None)


async def _fake_delete_all_for_user(steam_id):
    for jti in [j for j, (sid, _) in REFRESH_DB.items() if sid == steam_id]:
        del REFRESH_DB[jti]


@pytest.fixture(autouse=True)
def _fake_refresh_repo(monkeypatch):
    """Ningún test habla con Supabase por el store de refresh."""
    REFRESH_DB.clear()
    monkeypatch.setattr(refresh_repo, "save", _fake_save)
    monkeypatch.setattr(refresh_repo, "consume", _fake_consume)
    monkeypatch.setattr(refresh_repo, "revoke", _fake_revoke)
    monkeypatch.setattr(refresh_repo, "delete_all_for_user", _fake_delete_all_for_user)
    yield
    REFRESH_DB.clear()


@pytest.fixture(autouse=True)
def _clean_auth_stores():
    """Los stores de auth son dicts de módulo y no se limpian solos entre tests.

    El rate limiter es lo que de verdad obliga a esto: son RATE_LIMIT_CALLS
    (10) por IP y ventana, y todas las peticiones de TestClient llegan desde la
    misma ("testclient"). Sin limpiar, el test número 11 que toque /auth/token
    o /auth/review-login recibe un 429 que no tiene nada que ver con lo que
    estaba comprobando, y el fichero pasa o falla según el orden de ejecución.
    """
    for store in (_nonces, _auth_codes, _rate_store):
        store.clear()
    yield
    for store in (_nonces, _auth_codes, _rate_store):
        store.clear()


@pytest.fixture
def client(monkeypatch):
    # Skip the real ByMykel static-image fetch that main.py's lifespan performs on startup.
    monkeypatch.setattr(main, "_fetch_static_images", AsyncMock())

    app.dependency_overrides[require_jwt] = lambda: {"sub": STEAM_ID, "type": "access"}
    _inventory_cache.clear()
    _inventory_refresh_cooldown.clear()

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def steam_api(client, monkeypatch):
    """CAL-09: steamwebapi, ByMykel, frankfurter y Steam News simulados por HTTP, con
    todas las cachés en memoria vacías y el `_history_limiter` sin esperas."""
    import stores
    from steam import services
    from tests.steam_fake import FakeUpstream

    def _clear_caches():
        for name, value in vars(stores).items():
            if name.startswith("_") and not name.startswith("__") and isinstance(value, dict):
                value.clear()

    fake = FakeUpstream()
    _clear_caches()
    monkeypatch.setattr(app.state, "http_client", fake.client())
    monkeypatch.setattr(services._history_limiter, "acquire", AsyncMock())
    yield fake
    _clear_caches()
