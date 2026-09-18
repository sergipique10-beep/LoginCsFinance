from unittest.mock import AsyncMock

from alerts import router as alerts_router
from alerts import service as alerts_service
from tests.conftest import STEAM_ID

BODY = {"market_hash_name": "AK-47 | Redline (Field-Tested)", "direction": "below", "threshold": 40}


# ── GET /alerts ───────────────────────────────────────────────────────────────

def test_list_returns_only_the_callers_alerts(client, monkeypatch):
    mock = AsyncMock(return_value=[{"id": 1}])
    monkeypatch.setattr(alerts_router.repo, "list_for_user", mock)

    resp = client.get("/alerts")

    assert resp.status_code == 200
    assert resp.json() == {"alerts": [{"id": 1}]}
    mock.assert_awaited_once_with(STEAM_ID)


# ── POST /alerts ──────────────────────────────────────────────────────────────

def test_create_uses_jwt_owner_and_ignores_steam_id_in_body(client, monkeypatch):
    mock = AsyncMock(return_value={"id": 7})
    monkeypatch.setattr(alerts_service, "create_alert", mock)

    resp = client.post("/alerts", json={**BODY, "steam_id": "otro-usuario"})

    assert resp.status_code == 201
    assert resp.json() == {"id": 7}
    _, steam_id, name, direction, threshold = mock.await_args.args
    assert (steam_id, name, direction, threshold) == (STEAM_ID, BODY["market_hash_name"], "below", 40)


def test_create_rejects_invalid_direction(client):
    assert client.post("/alerts", json={**BODY, "direction": "sideways"}).status_code == 422


def test_create_rejects_non_positive_threshold(client):
    assert client.post("/alerts", json={**BODY, "threshold": 0}).status_code == 422
    assert client.post("/alerts", json={**BODY, "threshold": -1}).status_code == 422


def test_create_rejects_too_long_name(client):
    resp = client.post("/alerts", json={**BODY, "market_hash_name": "x" * 201})
    assert resp.status_code == 422


def test_create_maps_limit_to_422(client, monkeypatch):
    monkeypatch.setattr(alerts_service, "create_alert", AsyncMock(side_effect=alerts_service.LimitReached("max")))
    assert client.post("/alerts", json=BODY).status_code == 422


def test_create_maps_duplicate_to_409(client, monkeypatch):
    monkeypatch.setattr(alerts_service, "create_alert", AsyncMock(side_effect=alerts_service.Duplicate("dup")))
    assert client.post("/alerts", json=BODY).status_code == 409


def test_create_maps_unknown_item_to_404(client, monkeypatch):
    monkeypatch.setattr(alerts_service, "create_alert", AsyncMock(side_effect=alerts_service.UnknownItem("?")))
    assert client.post("/alerts", json=BODY).status_code == 404


# ── DELETE /alerts/{id} ───────────────────────────────────────────────────────

def test_delete_scopes_by_owner(client, monkeypatch):
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(alerts_router.repo, "delete", mock)

    resp = client.delete("/alerts/5")

    assert resp.status_code == 200
    mock.assert_awaited_once_with(5, STEAM_ID)


def test_delete_returns_404_when_not_owned_or_missing(client, monkeypatch):
    monkeypatch.setattr(alerts_router.repo, "delete", AsyncMock(return_value=False))

    assert client.delete("/alerts/5").status_code == 404


# ── POST /internal/alerts-tick ────────────────────────────────────────────────

def test_tick_requires_token(client, monkeypatch):
    monkeypatch.setattr(alerts_router, "ALERTS_TICK_TOKEN", "secret123")
    assert client.post("/internal/alerts-tick").status_code == 401


def test_tick_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr(alerts_router, "ALERTS_TICK_TOKEN", "secret123")
    assert client.post("/internal/alerts-tick", headers={"X-Alerts-Tick-Token": "wrong"}).status_code == 401


def test_tick_is_disabled_when_token_unset(client, monkeypatch):
    monkeypatch.setattr(alerts_router, "ALERTS_TICK_TOKEN", "")
    assert client.post("/internal/alerts-tick", headers={"X-Alerts-Tick-Token": ""}).status_code == 401


def test_tick_non_ascii_token_is_401_not_500(client, monkeypatch):
    """SEC-04/05: compare_digest sobre str revienta con un acento → 500 pre-auth."""
    monkeypatch.setattr(alerts_router, "ALERTS_TICK_TOKEN", "secret123")
    # Bytes latin-1: es lo que llega por el cable y lo que Starlette decodifica a str.
    resp = client.post(
        "/internal/alerts-tick",
        headers={b"X-Alerts-Tick-Token": "secreté".encode("latin-1")},
    )
    assert resp.status_code == 401


def test_tick_runs_evaluation_with_valid_token(client, monkeypatch):
    monkeypatch.setattr(alerts_router, "ALERTS_TICK_TOKEN", "secret123")
    mock = AsyncMock(return_value={"evaluated": 3, "triggered": 1, "sent": 1, "errors": 0, "pendientes": 0})
    monkeypatch.setattr(alerts_service, "evaluate_alerts", mock)

    resp = client.post("/internal/alerts-tick", headers={"X-Alerts-Tick-Token": "secret123"})

    assert resp.status_code == 200
    assert resp.json()["triggered"] == 1
    mock.assert_awaited_once()
