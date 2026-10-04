"""
Business logic for push notifications: registering FCM tokens, broadcasting
via Firebase Admin SDK, and detecting new CS2 news to notify about.
"""
import asyncio
import json
import logging

import httpx
import firebase_admin
from firebase_admin import credentials, exceptions as fb_exceptions, messaging

from settings import FIREBASE_SERVICE_ACCOUNT_JSON
from steam.mappers.news import _clean_news_content
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


async def register_token(token: str, platform: str, steam_id: str) -> None:
    await repo.register_device_token(token, platform, steam_id)


# FCM rechaza un MulticastMessage con más de 500 tokens.
_FCM_MULTICAST_LIMIT = 500

# Canal Android. Tiene que coincidir con el que crea el frontend
# (FirebaseMessaging.createChannel) y con default_notification_channel_id del
# manifest; si no existe en el dispositivo, Android usa el canal por defecto.
ANDROID_CHANNEL_ID = "cs_finance"

# Errores de FCM que identifican al TOKEN como muerto, nunca al payload:
# app desinstalada / token caducado, o token de otro proyecto Firebase.
_TOKEN_DEAD_ERRORS = (messaging.UnregisteredError, messaging.SenderIdMismatchError)


def _is_prunable(exc: Exception | None, any_success: bool) -> bool:
    if isinstance(exc, _TOKEN_DEAD_ERRORS):
        return True
    # InvalidArgumentError es permanente para un token malformado, pero también
    # lo lanza un payload inválido — y en ese caso fallan TODOS los tokens a la
    # vez. Solo se poda si algún envío del mismo lote tuvo éxito: eso descarta
    # que el problema sea el mensaje.
    return isinstance(exc, fb_exceptions.InvalidArgumentError) and any_success


async def send_to_tokens(tokens: list[str], title: str, body: str, data: dict[str, str]) -> dict[str, int]:
    """Envía la misma push a una lista concreta de tokens y poda los muertos.

    Única función de envío del backend: el broadcast de noticias y las push
    personalizadas (alertas de precio) pasan por aquí.
    """
    if not tokens:
        return {"sent": 0, "failed": 0, "pruned": 0}

    def _do():
        app = _get_firebase_app()
        responses = []
        for i in range(0, len(tokens), _FCM_MULTICAST_LIMIT):
            message = messaging.MulticastMessage(
                notification=messaging.Notification(title=title, body=body),
                data=data,
                android=messaging.AndroidConfig(
                    notification=messaging.AndroidNotification(channel_id=ANDROID_CHANNEL_ID),
                ),
                tokens=tokens[i:i + _FCM_MULTICAST_LIMIT],
            )
            responses.extend(messaging.send_each_for_multicast(message, app=app).responses)
        return responses

    responses = await asyncio.to_thread(_do)

    sent = sum(1 for r in responses if r.success)
    failed = len(responses) - sent

    invalid = [
        tokens[i]
        for i, r in enumerate(responses)
        if not r.success and _is_prunable(r.exception, any_success=sent > 0)
    ]
    if invalid:
        logger.info("[notifications] pruning %d dead token(s)", len(invalid))
        await repo.delete_device_tokens(invalid)

    return {"sent": sent, "failed": failed, "pruned": len(invalid)}


async def send_broadcast(title: str, body: str, data: dict[str, str]) -> dict[str, int]:
    return await send_to_tokens(await repo.list_device_tokens(), title, body, data)


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

    if len(new_items) == 1:
        item = new_items[0]
        title = item.get("title", "CS2 News")[:100]
        body = _clean_news_content(item.get("contents", ""), max_chars=140) or title
        await send_broadcast(
            title=title,
            body=body,
            data={"newsId": str(item["gid"]), "url": item.get("url", "")},
        )
    elif new_items:
        title = f"CS2 News — {len(new_items)} nuevas actualizaciones"
        body = "\n".join(
            f"• {item.get('title', 'Sin título')[:60]}" for item in new_items[:5]
        )
        await send_broadcast(
            title=title,
            body=body,
            data={"newsId": str(new_items[0]["gid"]), "url": new_items[0].get("url", "")},
        )

    await repo.mark_news_notified(new_gids)
    return {"notified": len(new_items)}
