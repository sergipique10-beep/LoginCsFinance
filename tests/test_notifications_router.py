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
