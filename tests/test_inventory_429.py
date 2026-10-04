"""PERF-14 — degradación elegante del inventario ante un 429 de steamwebapi.

429 = límite por minuto (transitorio): snapshot + reintento en segundo plano.
402 = cuota mensual agotada: snapshot si lo hay, pero NUNCA reintento.
"""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from steam.errors import QuotaExhausted
from steam.routes import items as items_routes
from steam.errors import RateLimited
from stores import _inventory_cache
from tests.conftest import SNAPSHOT_DB, STEAM_ID

SNAP_ITEMS = [{"name": "AK-47 | Redline"}]
SNAP_AT = "2026-10-02T08:00:00+00:00"
FRESH = [{"name": "AWP | Asiimov"}]


@pytest.fixture(autouse=True)
def _clean_state():
    _inventory_cache.clear()   # los tests sin `client` no pasan por su limpieza
    items_routes._retry_tasks.clear()
    items_routes._recent_429.clear()
    yield
    items_routes._retry_tasks.clear()
    items_routes._recent_429.clear()


def _rate_limited(monkeypatch, retry_after=3.0):
    monkeypatch.setattr(items_routes, "_fetch_fresh_inventory",
                        AsyncMock(side_effect=RateLimited(retry_after)))
    schedule = MagicMock()
    monkeypatch.setattr(items_routes, "_schedule_retry", schedule)
    return schedule


def _quota_exhausted(monkeypatch):
    monkeypatch.setattr(items_routes, "_fetch_fresh_inventory",
                        AsyncMock(side_effect=QuotaExhausted("402")))
    schedule = MagicMock()
    monkeypatch.setattr(items_routes, "_schedule_retry", schedule)
    return schedule


# ── 429: snapshot con timestamp + reintento programado ───────────────────────

def test_429_serves_snapshot_with_timestamp_and_schedules_retry(client, monkeypatch):
    SNAPSHOT_DB[STEAM_ID] = (SNAP_ITEMS, SNAP_AT)
    schedule = _rate_limited(monkeypatch, retry_after=3.0)

    resp = client.get("/inventory")

    assert resp.status_code == 200
    assert resp.json() == SNAP_ITEMS                       # el cuerpo sigue siendo la lista
    assert resp.headers["X-Inventory-Stale"] == "1"
    assert resp.headers["X-Inventory-Captured-At"] == SNAP_AT
    schedule.assert_called_once()
    assert schedule.call_args.args[1:] == (STEAM_ID, 3.0)  # (request, steam_id, retry_after)
    assert STEAM_ID not in _inventory_cache                # el snapshot no se hace pasar por fresco


def test_429_on_refresh_also_serves_snapshot_and_keeps_cooldown_free(client, monkeypatch):
    SNAPSHOT_DB[STEAM_ID] = (SNAP_ITEMS, SNAP_AT)
    schedule = _rate_limited(monkeypatch)

    resp = client.post("/inventory/refresh")

    assert resp.status_code == 200
    assert resp.headers["X-Inventory-Stale"] == "1"
    schedule.assert_called_once()
    # el refresh fallido no gasta el cooldown de 1 h del botón
    from stores import _inventory_refresh_cooldown
    assert STEAM_ID not in _inventory_refresh_cooldown


def test_429_without_snapshot_keeps_the_error_but_still_schedules_retry(client, monkeypatch):
    schedule = _rate_limited(monkeypatch)

    resp = client.get("/inventory")

    assert resp.status_code == 429
    schedule.assert_called_once()      # sin snapshot no hay qué enseñar, pero se calienta la caché


def test_fresh_read_stores_snapshot(client, monkeypatch):
    monkeypatch.setattr(items_routes, "_fetch_fresh_inventory", AsyncMock(return_value=FRESH))

    resp = client.get("/inventory")

    assert resp.status_code == 200
    assert "X-Inventory-Stale" not in resp.headers
    assert SNAPSHOT_DB[STEAM_ID][0] == FRESH


def test_snapshot_failure_never_breaks_a_good_read(client, monkeypatch):
    monkeypatch.setattr(items_routes, "_fetch_fresh_inventory", AsyncMock(return_value=FRESH))
    monkeypatch.setattr(items_routes.inventory_snapshot_repo, "save",
                        AsyncMock(side_effect=RuntimeError("supabase caído")))

    assert client.get("/inventory").json() == FRESH


def test_no_new_steam_call_while_a_retry_is_in_flight(client, monkeypatch):
    """Cada GET extra mientras hay límite sería otro 429: se sirve el snapshot sin llamar."""
    SNAPSHOT_DB[STEAM_ID] = (SNAP_ITEMS, SNAP_AT)
    fetch = AsyncMock(return_value=FRESH)
    monkeypatch.setattr(items_routes, "_fetch_fresh_inventory", fetch)
    items_routes._retry_tasks[STEAM_ID] = MagicMock()

    resp = client.get("/inventory")

    assert resp.headers["X-Inventory-Stale"] == "1"
    fetch.assert_not_awaited()


# ── 402: sigue abortando, sin reintentos ─────────────────────────────────────

def test_402_never_schedules_a_retry_even_with_snapshot(client, monkeypatch):
    SNAPSHOT_DB[STEAM_ID] = (SNAP_ITEMS, SNAP_AT)
    schedule = _quota_exhausted(monkeypatch)

    resp = client.get("/inventory")

    assert resp.status_code == 200 and resp.headers["X-Inventory-Stale"] == "1"
    schedule.assert_not_called()


def test_402_without_snapshot_is_503_upstream_quota_and_no_retry(client, monkeypatch):
    schedule = _quota_exhausted(monkeypatch)

    resp = client.get("/inventory")

    assert resp.status_code == 503   # SEC-16: antes 502, que el front leía como «servidor caído»
    assert resp.json()["detail"]["code"] == "upstream_quota"
    schedule.assert_not_called()


# ── mapeo HTTP → excepción ───────────────────────────────────────────────────

def _request_returning(response):
    client = SimpleNamespace(get=AsyncMock(return_value=response))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http_client=client)))


def test_fetch_maps_429_with_retry_after():
    req = _request_returning(httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(RateLimited) as exc:
        asyncio.run(items_routes._fetch_fresh_inventory(req, STEAM_ID))
    assert exc.value.retry_after == 7.0


def test_fetch_maps_429_without_or_with_garbage_retry_after():
    for headers in ({}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}):
        req = _request_returning(httpx.Response(429, headers=headers))
        with pytest.raises(RateLimited) as exc:
            asyncio.run(items_routes._fetch_fresh_inventory(req, STEAM_ID))
        assert exc.value.retry_after is None


def test_fetch_maps_402_to_quota_exhausted():
    req = _request_returning(httpx.Response(402))
    with pytest.raises(QuotaExhausted):
        asyncio.run(items_routes._fetch_fresh_inventory(req, STEAM_ID))


# ── backoff ──────────────────────────────────────────────────────────────────

def test_backoff_grows_is_capped_and_respects_retry_after(monkeypatch):
    monkeypatch.setattr(items_routes, "INVENTORY_429_BACKOFF_BASE", 5.0)
    monkeypatch.setattr(items_routes, "INVENTORY_429_BACKOFF_CAP", 120.0)

    for attempt, exp in ((0, 5), (1, 10), (2, 20), (10, 120)):
        for _ in range(50):
            assert exp / 2 <= items_routes._backoff(attempt, None) <= exp
    assert items_routes._backoff(0, 45.0) >= 45.0       # Retry-After manda sobre el backoff


# ── el reintento en sí ───────────────────────────────────────────────────────

def _run_retry(monkeypatch, side_effects, retries=4):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(items_routes.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(items_routes, "INVENTORY_429_MAX_RETRIES", retries)
    fetch = AsyncMock(side_effect=side_effects)
    monkeypatch.setattr(items_routes, "_fetch_fresh_inventory", fetch)
    asyncio.run(items_routes._retry_inventory(object(), STEAM_ID, 2.0))
    return fetch, sleeps


def test_retry_recovers_after_a_second_429_and_refreshes_cache_and_snapshot(monkeypatch):
    fetch, sleeps = _run_retry(monkeypatch, [RateLimited(1.0), FRESH])

    assert fetch.await_count == 2
    assert sleeps[0] >= 2.0                                 # Retry-After inicial respetado
    assert _inventory_cache[STEAM_ID][0] == FRESH
    assert SNAPSHOT_DB[STEAM_ID][0] == FRESH


def test_retry_gives_up_after_the_configured_limit(monkeypatch):
    fetch, sleeps = _run_retry(monkeypatch, RateLimited(None), retries=3)

    assert fetch.await_count == 3 and len(sleeps) == 3
    assert STEAM_ID not in _inventory_cache


def test_retry_aborts_on_402_without_further_attempts(monkeypatch):
    fetch, sleeps = _run_retry(monkeypatch, QuotaExhausted("402"))

    assert fetch.await_count == 1
    assert STEAM_ID not in _inventory_cache


# ── logs: son lo que dice si los 429 son recurrentes ─────────────────────────

def test_429_log_carries_context_and_hourly_count(client, monkeypatch, caplog):
    _rate_limited(monkeypatch, retry_after=3.0)
    SNAPSHOT_DB[STEAM_ID] = (SNAP_ITEMS, SNAP_AT)

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        client.get("/inventory")
        client.post("/inventory/refresh")

    lines = [r.getMessage() for r in caplog.records if "[inventory-429]" in r.getMessage()]
    assert len(lines) == 2
    assert f"user={STEAM_ID}" in lines[0] and "origin=get" in lines[0]
    assert "retry_after=3.0" in lines[0] and "served=snapshot" in lines[0]
    assert "last_hour=1" in lines[0] and "last_hour=2" in lines[1]
