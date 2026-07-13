import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

import main
from main import app
from auth.service import require_jwt
from stores import _inventory_cache, _inventory_refresh_cooldown

STEAM_ID = "test_steam_id"


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
