import asyncio
from unittest.mock import AsyncMock

from firebase_admin import messaging
from firebase_admin.exceptions import InvalidArgumentError, UnavailableError

from notifications import service as notifications_service

RAW_NEWS = [
    {"gid": "111", "title": "Nuevo update CS2", "contents": "Contenido de prueba", "url": "https://example.com/1"},
    {"gid": "222", "title": "Otro parche", "contents": "Mas contenido", "url": "https://example.com/2"},
]


class FakeResult:
    def __init__(self, success, exception=None):
        self.success = success
        self.exception = exception


class FakeBatch:
    def __init__(self, responses):
        self.responses = responses


def _fake_fcm(monkeypatch, results):
    """Sustituye send_each_for_multicast. Devuelve la lista de MulticastMessage
    que recibió, para poder inspeccionar tokens y payload de cada lote."""
    monkeypatch.setattr(notifications_service, "_get_firebase_app", lambda: object())
    messages = []
    remaining = list(results)

    def fake_send(message, app):
        messages.append(message)
        batch, remaining[:] = remaining[: len(message.tokens)], remaining[len(message.tokens):]
        return FakeBatch(batch)

    monkeypatch.setattr(messaging, "send_each_for_multicast", fake_send)
    return messages


# ── check_and_notify_new_news ─────────────────────────────────────────────────

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


# ── send_broadcast (wrapper sobre todos los tokens) ───────────────────────────

def test_send_broadcast_sends_to_every_registered_token(monkeypatch):
    monkeypatch.setattr(notifications_service.repo, "list_device_tokens", AsyncMock(return_value=["tok-a", "tok-b"]))
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", AsyncMock())
    messages = _fake_fcm(monkeypatch, [FakeResult(True), FakeResult(True)])

    result = asyncio.run(notifications_service.send_broadcast("Title", "Body", {"k": "v"}))

    assert result == {"sent": 2, "failed": 0, "pruned": 0}
    assert messages[0].tokens == ["tok-a", "tok-b"]
    assert messages[0].notification.title == "Title"
    assert messages[0].notification.body == "Body"
    assert messages[0].data == {"k": "v"}


def test_send_broadcast_returns_zeros_with_no_tokens(monkeypatch):
    monkeypatch.setattr(notifications_service.repo, "list_device_tokens", AsyncMock(return_value=[]))
    delete_mock = AsyncMock()
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", delete_mock)

    result = asyncio.run(notifications_service.send_broadcast("Title", "Body", {}))

    assert result == {"sent": 0, "failed": 0, "pruned": 0}
    delete_mock.assert_not_awaited()


# ── send_to_tokens: payload y troceado ────────────────────────────────────────

def test_send_to_tokens_sets_android_channel(monkeypatch):
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", AsyncMock())
    messages = _fake_fcm(monkeypatch, [FakeResult(True)])

    asyncio.run(notifications_service.send_to_tokens(["tok-a"], "T", "B", {}))

    assert messages[0].android.notification.channel_id == notifications_service.ANDROID_CHANNEL_ID


def test_send_to_tokens_chunks_at_fcm_limit(monkeypatch):
    """FCM rechaza más de 500 tokens por MulticastMessage: 1001 tokens → 3 lotes."""
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", AsyncMock())
    tokens = [f"tok-{i}" for i in range(1001)]
    messages = _fake_fcm(monkeypatch, [FakeResult(True)] * 1001)

    result = asyncio.run(notifications_service.send_to_tokens(tokens, "T", "B", {}))

    assert [len(m.tokens) for m in messages] == [500, 500, 1]
    assert messages[2].tokens == ["tok-1000"]
    assert result == {"sent": 1001, "failed": 0, "pruned": 0}


def test_send_to_tokens_prunes_across_chunks_with_correct_token(monkeypatch):
    """El índice de la respuesta debe mapear al token correcto aunque esté en el segundo lote."""
    delete_mock = AsyncMock()
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", delete_mock)
    tokens = [f"tok-{i}" for i in range(501)]
    results = [FakeResult(True)] * 500 + [FakeResult(False, messaging.UnregisteredError("gone"))]
    _fake_fcm(monkeypatch, results)

    asyncio.run(notifications_service.send_to_tokens(tokens, "T", "B", {}))

    delete_mock.assert_awaited_once_with(["tok-500"])


# ── send_to_tokens: pruning (PUSH-04) ─────────────────────────────────────────

def test_send_to_tokens_prunes_unregistered(monkeypatch):
    delete_mock = AsyncMock()
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", delete_mock)
    _fake_fcm(monkeypatch, [FakeResult(True), FakeResult(False, messaging.UnregisteredError("gone"))])

    asyncio.run(notifications_service.send_to_tokens(["tok-a", "tok-b"], "T", "B", {}))

    delete_mock.assert_awaited_once_with(["tok-b"])


def test_send_to_tokens_prunes_sender_id_mismatch(monkeypatch):
    """Token de otro proyecto Firebase: permanente, se poda."""
    delete_mock = AsyncMock()
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", delete_mock)
    _fake_fcm(monkeypatch, [FakeResult(True), FakeResult(False, messaging.SenderIdMismatchError("otro"))])

    asyncio.run(notifications_service.send_to_tokens(["tok-a", "tok-b"], "T", "B", {}))

    delete_mock.assert_awaited_once_with(["tok-b"])


def test_send_to_tokens_keeps_tokens_on_transient_errors(monkeypatch):
    """UnavailableError / errores genéricos son transitorios: el token se queda."""
    delete_mock = AsyncMock()
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", delete_mock)
    _fake_fcm(monkeypatch, [
        FakeResult(True),
        FakeResult(False, UnavailableError("fcm down")),
        FakeResult(False, ValueError("boom")),
    ])

    result = asyncio.run(notifications_service.send_to_tokens(["a", "b", "c"], "T", "B", {}))

    assert result == {"sent": 1, "failed": 2, "pruned": 0}
    delete_mock.assert_not_awaited()


def test_send_to_tokens_prunes_invalid_argument_when_some_succeed(monkeypatch):
    """Si otros tokens del lote pasaron, el InvalidArgument es del token, no del payload."""
    delete_mock = AsyncMock()
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", delete_mock)
    _fake_fcm(monkeypatch, [FakeResult(True), FakeResult(False, InvalidArgumentError("bad token"))])

    asyncio.run(notifications_service.send_to_tokens(["tok-a", "tok-b"], "T", "B", {}))

    delete_mock.assert_awaited_once_with(["tok-b"])


def test_send_to_tokens_never_prunes_when_every_token_fails(monkeypatch):
    """Salvaguarda: un payload inválido hace fallar el 100 % con InvalidArgument.
    Podar ahí vaciaría device_tokens de golpe."""
    delete_mock = AsyncMock()
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", delete_mock)
    _fake_fcm(monkeypatch, [
        FakeResult(False, InvalidArgumentError("bad payload")),
        FakeResult(False, InvalidArgumentError("bad payload")),
    ])

    result = asyncio.run(notifications_service.send_to_tokens(["tok-a", "tok-b"], "T", "B", {}))

    assert result == {"sent": 0, "failed": 2, "pruned": 0}
    delete_mock.assert_not_awaited()


def test_send_to_tokens_unregistered_is_pruned_even_if_it_is_the_only_token(monkeypatch):
    """La salvaguarda del 100 % NO aplica a Unregistered: un usuario con un solo
    dispositivo que desinstala la app debe salir de la tabla."""
    delete_mock = AsyncMock()
    monkeypatch.setattr(notifications_service.repo, "delete_device_tokens", delete_mock)
    _fake_fcm(monkeypatch, [FakeResult(False, messaging.UnregisteredError("gone"))])

    asyncio.run(notifications_service.send_to_tokens(["tok-a"], "T", "B", {}))

    delete_mock.assert_awaited_once_with(["tok-a"])
