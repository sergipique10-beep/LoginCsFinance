from unittest.mock import AsyncMock

from stores import INVENTORY_REFRESH_COOLDOWN, _inventory_cache
from steam.routes import items as items_routes
from tests.conftest import STEAM_ID

FRESH_ITEMS = [{"name": "AK-47 | Redline"}]


def _patch_fetch(monkeypatch, items=FRESH_ITEMS):
    mock = AsyncMock(return_value=items)
    monkeypatch.setattr(items_routes, "_fetch_fresh_inventory", mock)
    return mock


def _freeze_time(monkeypatch, start=1000.0):
    fake_now = [start]
    monkeypatch.setattr(items_routes.time, "monotonic", lambda: fake_now[0])
    return fake_now


def test_refresh_success_returns_fresh_items_and_updates_cache(client, monkeypatch):
    fake_now = _freeze_time(monkeypatch)
    _patch_fetch(monkeypatch)

    resp = client.post("/inventory/refresh")

    assert resp.status_code == 200
    assert resp.json() == FRESH_ITEMS
    assert _inventory_cache[STEAM_ID] == (FRESH_ITEMS, fake_now[0])


def test_refresh_bypasses_fresh_23h_cache(client, monkeypatch):
    fake_now = _freeze_time(monkeypatch)
    _inventory_cache[STEAM_ID] = ([{"name": "stale item"}], fake_now[0])
    _patch_fetch(monkeypatch)

    resp = client.post("/inventory/refresh")

    assert resp.status_code == 200
    assert resp.json() == FRESH_ITEMS


def test_second_refresh_within_cooldown_returns_429(client, monkeypatch):
    _freeze_time(monkeypatch)
    _patch_fetch(monkeypatch)

    first = client.post("/inventory/refresh")
    second = client.post("/inventory/refresh")

    assert first.status_code == 200
    assert second.status_code == 429
    assert "retry" in second.json()["detail"].lower()


def test_refresh_allowed_again_after_cooldown_expires(client, monkeypatch):
    fake_now = _freeze_time(monkeypatch)
    _patch_fetch(monkeypatch)

    first = client.post("/inventory/refresh")
    fake_now[0] += INVENTORY_REFRESH_COOLDOWN + 1
    second = client.post("/inventory/refresh")

    assert first.status_code == 200
    assert second.status_code == 200


def test_get_inventory_still_uses_23h_cache_unaffected_by_refresh_endpoint(client, monkeypatch):
    fake_now = _freeze_time(monkeypatch)
    _patch_fetch(monkeypatch)
    fetch_mock = items_routes._fetch_fresh_inventory

    first = client.get("/inventory")
    second = client.get("/inventory")

    assert first.status_code == 200
    assert second.status_code == 200
    assert fetch_mock.await_count == 1  # second GET served from cache, no re-fetch
