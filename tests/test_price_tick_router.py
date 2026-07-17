from unittest.mock import AsyncMock

from steam.routes import market as market_routes


def test_price_tick_requires_token(client, monkeypatch):
    monkeypatch.setattr(market_routes, "PRICE_TICK_TOKEN", "secret123")
    resp = client.post("/internal/price-tick")
    assert resp.status_code == 401


def test_price_tick_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr(market_routes, "PRICE_TICK_TOKEN", "secret123")
    resp = client.post("/internal/price-tick", headers={"X-Price-Tick-Token": "nope"})
    assert resp.status_code == 401


def test_price_tick_runs_with_valid_token(client, monkeypatch):
    monkeypatch.setattr(market_routes, "PRICE_TICK_TOKEN", "secret123")
    monkeypatch.setattr(market_routes, "price_capture_run",
                        AsyncMock(return_value={"tracked_run": 3, "captured": 2,
                                               "skipped": 1, "errors": 0}))
    resp = client.post("/internal/price-tick", headers={"X-Price-Tick-Token": "secret123"})
    assert resp.status_code == 200
    assert resp.json() == {"tracked_run": 3, "captured": 2, "skipped": 1, "errors": 0}
