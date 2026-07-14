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


def test_send_broadcast_returns_counters(monkeypatch):
    from firebase_admin import messaging

    monkeypatch.setattr(
        notifications_service.repo, "list_device_tokens", AsyncMock(return_value=["tok-a", "tok-b", "tok-c"])
    )
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", AsyncMock())
    monkeypatch.setattr(notifications_service, "_get_firebase_app", lambda: object())

    class FakeResult:
        def __init__(self, success, exception=None):
            self.success = success
            self.exception = exception

    fake_batch_response = type(
        "FakeBatch",
        (),
        {
            "responses": [
                FakeResult(True),
                FakeResult(False, messaging.UnregisteredError("gone")),
                FakeResult(False, ValueError("boom")),
            ]
        },
    )()
    monkeypatch.setattr(messaging, "send_each_for_multicast", lambda message, app: fake_batch_response)

    result = asyncio.run(notifications_service.send_broadcast("Title", "Body", {}))

    assert result == {"sent": 1, "failed": 2, "pruned": 1}


def test_send_broadcast_returns_zeros_with_no_tokens(monkeypatch):
    monkeypatch.setattr(notifications_service.repo, "list_device_tokens", AsyncMock(return_value=[]))

    result = asyncio.run(notifications_service.send_broadcast("Title", "Body", {}))

    assert result == {"sent": 0, "failed": 0, "pruned": 0}
