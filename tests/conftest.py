import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

import main
from main import app
from auth.service import require_jwt
from stores import (
    _auth_codes, _inventory_cache, _inventory_refresh_cooldown,
    _nonces, _rate_store, _refresh_store,
)

STEAM_ID = "test_steam_id"


@pytest.fixture(autouse=True)
def _clean_auth_stores():
    """Los stores de auth son dicts de módulo y no se limpian solos entre tests.

    El rate limiter es lo que de verdad obliga a esto: son RATE_LIMIT_CALLS
    (10) por IP y ventana, y todas las peticiones de TestClient llegan desde la
    misma ("testclient"). Sin limpiar, el test número 11 que toque /auth/token
    o /auth/review-login recibe un 429 que no tiene nada que ver con lo que
    estaba comprobando, y el fichero pasa o falla según el orden de ejecución.
    """
    for store in (_nonces, _auth_codes, _refresh_store, _rate_store):
        store.clear()
    yield
    for store in (_nonces, _auth_codes, _refresh_store, _rate_store):
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
