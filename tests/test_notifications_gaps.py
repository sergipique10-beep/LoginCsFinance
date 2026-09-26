"""CAL-03: huecos de cobertura en notificaciones.

La rama agrupada de `check_and_notify_new_news` solo corre cuando Valve publica
2+ noticias entre dos ticks: casi nunca en desarrollo y justo en el día de parche
grande, con todos los usuarios delante.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from notifications import repo as notifications_repo
from notifications import service as notifications_service


def _news(n: int) -> list[dict]:
    return [
        {"gid": str(100 + i), "title": f"Noticia {i} " + "x" * 80, "contents": f"cuerpo {i}",
         "url": f"https://store.steampowered.com/news/{100 + i}"}
        for i in range(n)
    ]


def _prepare(monkeypatch, items: list[dict], new_gids: list[str]) -> AsyncMock:
    monkeypatch.setattr(notifications_service, "_fetch_raw_news", AsyncMock(return_value=items))
    monkeypatch.setattr(notifications_service.repo, "filter_new_news_gids", AsyncMock(return_value=new_gids))
    monkeypatch.setattr(notifications_service.repo, "mark_news_notified", AsyncMock())
    send = AsyncMock(return_value={"sent": 1, "failed": 0, "pruned": 0})
    monkeypatch.setattr(notifications_service, "send_broadcast", send)
    return send


@pytest.mark.asyncio
async def test_several_new_news_are_grouped_in_one_push(monkeypatch):
    items = _news(3)
    send = _prepare(monkeypatch, items, [i["gid"] for i in items])

    out = await notifications_service.check_and_notify_new_news(MagicMock())

    assert out == {"notified": 3}
    send.assert_awaited_once()
    kwargs = send.await_args.kwargs
    assert kwargs["title"] == "CS2 News — 3 nuevas actualizaciones"
    assert kwargs["body"].count("• ") == 3
    assert all(len(line) <= 2 + 60 for line in kwargs["body"].split("\n"))   # títulos recortados a 60
    assert kwargs["data"] == {"newsId": "100", "url": "https://store.steampowered.com/news/100"}


@pytest.mark.asyncio
async def test_grouped_push_lists_at_most_five_bullets(monkeypatch):
    items = _news(7)
    send = _prepare(monkeypatch, items, [i["gid"] for i in items])

    out = await notifications_service.check_and_notify_new_news(MagicMock())

    assert out == {"notified": 7}                      # se marcan las 7 como notificadas
    assert send.await_args.kwargs["title"] == "CS2 News — 7 nuevas actualizaciones"
    assert send.await_args.kwargs["body"].count("• ") == 5


def test_firebase_app_requires_the_service_account(monkeypatch):
    monkeypatch.setattr(notifications_service, "_firebase_app", None)
    monkeypatch.setattr(notifications_service, "FIREBASE_SERVICE_ACCOUNT_JSON", "")

    with pytest.raises(RuntimeError, match="FIREBASE_SERVICE_ACCOUNT_JSON"):
        notifications_service._get_firebase_app()


@pytest.mark.asyncio
async def test_register_device_token_upserts_by_token_and_overwrites_owner(monkeypatch):
    table = MagicMock()
    client = MagicMock()
    client.table.return_value = table
    monkeypatch.setattr(notifications_repo, "get_supabase", lambda: client)

    await notifications_repo.register_device_token("tok-1", "android", "7656")

    client.table.assert_called_once_with("device_tokens")
    table.upsert.assert_called_once_with(
        {"token": "tok-1", "platform": "android", "steam_id": "7656"}, on_conflict="token",
    )
    table.upsert.return_value.execute.assert_called_once()
