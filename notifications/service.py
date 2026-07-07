"""
Business logic for push notifications: registering FCM tokens, broadcasting
via Firebase Admin SDK, and detecting new CS2 news to notify about.
"""
import asyncio
import json
import logging

import httpx
import firebase_admin
from firebase_admin import credentials, messaging

from settings import FIREBASE_SERVICE_ACCOUNT_JSON
from steam.mappers import _clean_news_content
from . import repo

logger = logging.getLogger("uvicorn.error")

STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
_NEWS_TICK_COUNT = 10

_firebase_app: firebase_admin.App | None = None


def _get_firebase_app() -> firebase_admin.App:
    global _firebase_app
    if _firebase_app is None:
        if not FIREBASE_SERVICE_ACCOUNT_JSON:
            raise RuntimeError(
                "FIREBASE_SERVICE_ACCOUNT_JSON no configurada — no se pueden enviar push"
            )
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
        _firebase_app = firebase_admin.initialize_app(cred, name="cs-finance-notifications")
    return _firebase_app


async def register_token(token: str, platform: str) -> None:
    await repo.register_device_token(token, platform)


async def send_broadcast(title: str, body: str, data: dict[str, str]) -> None:
    tokens = await repo.list_device_tokens()
    if not tokens:
        return

    def _do():
        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            data=data,
            tokens=tokens,
        )
        return messaging.send_each_for_multicast(message, app=_get_firebase_app())

    response = await asyncio.to_thread(_do)

    invalid = [
        tokens[i]
        for i, r in enumerate(response.responses)
        if not r.success and isinstance(r.exception, messaging.UnregisteredError)
    ]
    if invalid:
        logger.info("[notifications] pruning %d unregistered token(s)", len(invalid))
        await repo.delete_device_tokens(invalid)


async def _fetch_raw_news(http_client: httpx.AsyncClient, count: int = _NEWS_TICK_COUNT) -> list[dict]:
    resp = await http_client.get(
        STEAM_NEWS_URL,
        params={"appid": 730, "count": count, "format": "json"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json().get("appnews", {}).get("newsitems", [])


async def check_and_notify_new_news(http_client: httpx.AsyncClient) -> dict:
    newsitems = await _fetch_raw_news(http_client)
    gids = [str(item["gid"]) for item in newsitems if item.get("gid")]

    new_gids = await repo.filter_new_news_gids(gids)
    if not new_gids:
        return {"notified": 0}

    new_gids_set = set(new_gids)
    new_items = [item for item in newsitems if str(item.get("gid", "")) in new_gids_set]

    for item in new_items:
        title = item.get("title", "CS2 News")[:100]
        body = _clean_news_content(item.get("contents", ""), max_chars=140) or title
        await send_broadcast(
            title=title,
            body=body,
            data={"newsId": str(item["gid"]), "url": item.get("url", "")},
        )

    await repo.mark_news_notified(new_gids)
    return {"notified": len(new_items)}
