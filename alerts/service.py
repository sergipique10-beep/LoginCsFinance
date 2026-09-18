"""
Alertas de precio por skin: creación validada y evaluación periódica (tick).

Una alerta es de un solo disparo. El tick lee las activas menos-recientemente
evaluadas (hasta ALERTS_LOOKUP_CAP), consulta el precio spot UNA vez por skin
a través del mismo lookup y limiter que el price-tick, y a las que cumplen la
condición las marca disparadas ANTES de mandar la push: si el envío revienta
a medias, el siguiente tick no las reenvía. Se sacrifica un aviso perdido a
cambio de no duplicar nunca.
"""
import logging

import httpx

from settings import ALERTS_LOOKUP_CAP, ALERTS_MAX_PER_USER
from steam import price_capture
from steam import price_history_repo
from notifications import repo as notif_repo
from notifications.service import send_to_tokens
from . import repo

logger = logging.getLogger("uvicorn.error")

MAX_NAME_LEN = 200


class AlertError(Exception):
    """Base: el router la traduce a HTTP."""


class LimitReached(AlertError):
    pass


class Duplicate(AlertError):
    pass


class UnknownItem(AlertError):
    pass


def condition_met(direction: str, threshold: float, price: float) -> bool:
    # Inclusivo en el borde: «avísame cuando llegue a 40» incluye 40.
    return price >= threshold if direction == "above" else price <= threshold


async def create_alert(
    http_client: httpx.AsyncClient,
    steam_id: str,
    market_hash_name: str,
    direction: str,
    threshold: float,
) -> dict:
    threshold = round(threshold, 2)
    if await repo.count_active(steam_id) >= ALERTS_MAX_PER_USER:
        raise LimitReached(f"Máximo {ALERTS_MAX_PER_USER} alertas activas")
    if await repo.exists_active(steam_id, market_hash_name, direction, threshold):
        raise Duplicate("Ya existe una alerta activa idéntica")

    # El nombre tiene que ser una skin real. Si ya la seguimos, listo; si no,
    # un lookup la resuelve (cuesta 1 req de cuota, solo en creación) y la
    # registra en tracked_skins para que entre en la captura diaria.
    if not await price_history_repo.is_tracked(market_hash_name):
        try:
            item = await price_capture._lookup_item(http_client, market_hash_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[alerts] lookup falló para %r: %s", market_hash_name, exc)
            item = {}
        if price_capture._canonical_price(item) is None:
            raise UnknownItem(f"No se encontró la skin {market_hash_name!r}")
        await price_history_repo.register_tracked([market_hash_name], "alert")

    return await repo.create(steam_id, market_hash_name, direction, threshold)


def _format_push(alert: dict, price: float) -> tuple[str, str, dict[str, str]]:
    above = alert["direction"] == "above"
    threshold = float(alert["threshold"])
    title = alert["market_hash_name"]
    body = (
        f"Ha {'subido' if above else 'bajado'} a {price:.2f} $ "
        f"(tu alerta: {'≥' if above else '≤'} {threshold:.2f} $)"
    )
    data = {
        "type": "price_alert",
        "alertId": str(alert["id"]),
        "marketHashName": alert["market_hash_name"],
        "price": f"{price:.2f}",
    }
    return title, body, data


async def evaluate_alerts(http_client: httpx.AsyncClient) -> dict:
    """Un lote de evaluación. Devuelve contadores; `pendientes` es lo que queda
    activo fuera de este lote (rota en los siguientes ticks)."""
    total_active = await repo.count_active()
    alerts = await repo.fetch_active(ALERTS_LOOKUP_CAP)

    # ponytail: una req por skin y tick, tope ALERTS_LOOKUP_CAP (una ventana del
    # limiter). Si crece el nº de skins con alertas, el siguiente paso es leer
    # primero market_trending / market_movers (refrescadas cada 15 min, gratis)
    # y hacer lookup solo del resto.
    prices: dict[str, float] = {}
    errors = 0
    for name in dict.fromkeys(a["market_hash_name"] for a in alerts):
        try:
            item = await price_capture._lookup_item(http_client, name)
        except Exception as exc:  # noqa: BLE001 — best-effort: un fallo no aborta el lote
            errors += 1
            logger.warning("[alerts] lookup falló para %r: %s", name, exc)
            continue
        price = price_capture._canonical_price(item)
        if price is not None:
            prices[name] = price

    triggered = [
        a for a in alerts
        if a["market_hash_name"] in prices
        and condition_met(a["direction"], float(a["threshold"]), prices[a["market_hash_name"]])
    ]

    tokens_by_user = await notif_repo.list_device_tokens_for(
        list(dict.fromkeys(a["steam_id"] for a in triggered))
    )

    sent = 0
    for alert in triggered:
        price = prices[alert["market_hash_name"]]
        try:
            await repo.mark_triggered(alert["id"], price)
        except Exception as exc:  # noqa: BLE001
            # Sin marca no se envía: mejor un aviso que llega al siguiente tick
            # que uno duplicado.
            errors += 1
            logger.warning("[alerts] mark_triggered falló para %s: %s", alert["id"], exc)
            continue
        tokens = tokens_by_user.get(alert["steam_id"], [])
        if not tokens:
            continue
        title, body, data = _format_push(alert, price)
        try:
            result = await send_to_tokens(tokens, title, body, data)
            sent += result["sent"]
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("[alerts] envío falló para %s: %s", alert["id"], exc)

    await repo.mark_checked([a["id"] for a in alerts])

    out = {
        "evaluated": len(alerts),
        "triggered": len(triggered),
        "sent": sent,
        "errors": errors,
        "pendientes": max(total_active - len(alerts), 0),
    }
    logger.info("[alerts] %s", out)
    return out
