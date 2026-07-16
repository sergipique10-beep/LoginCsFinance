from types import SimpleNamespace
from unittest.mock import AsyncMock

from main import app
from steam.routes import items as items_routes


class _FakeResponse:
    status_code = 200

    def json(self):
        return []


def _patch_http_client(monkeypatch):
    """Replace the shared httpx client with a mock that records the /inventory call."""
    monkeypatch.setattr(items_routes, "_enrich_market_prices", AsyncMock(return_value=[]))
    get_mock = AsyncMock(return_value=_FakeResponse())
    app.state.http_client = SimpleNamespace(get=get_mock, aclose=AsyncMock())
    return get_mock


def test_refresh_asks_steamwebapi_to_bypass_its_own_cache(client, monkeypatch):
    """steamwebapi serves its own cached inventory copy unless no_cache is set.

    Without it, items acquired after their snapshot never reach us — not even via
    POST /inventory/refresh, which only bypasses *our* 23h cache.
    """
    get_mock = _patch_http_client(monkeypatch)

    resp = client.post("/inventory/refresh")

    assert resp.status_code == 200
    assert get_mock.await_args.kwargs["params"]["no_cache"] == 1


def test_get_inventory_asks_steamwebapi_to_bypass_its_own_cache(client, monkeypatch):
    get_mock = _patch_http_client(monkeypatch)

    resp = client.get("/inventory")

    assert resp.status_code == 200
    assert get_mock.await_args.kwargs["params"]["no_cache"] == 1
