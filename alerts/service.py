"""
Alertas de precio por skin: creación validada y evaluación periódica (tick).

Una alerta es de un solo disparo. El tick lee las activas menos-recientemente
evaluadas (hasta ALERTS_LOOKUP_CAP) y resuelve el precio de cada skin, en este
orden (PERF-09):

1. `market_movers` / `market_trending` (Supabase, refrescadas por market-tick):
   gratis y para la mayoría de skins con alerta. Se aceptan filas de hasta
   CACHED_PRICE_MAX_AGE; más viejas cuentan como ausentes.
2. `/item` de steamwebapi solo para lo que no cubra (1), y como mucho UNA vez
   al día por skin: `last_checked_at` hace de cursor, sin estado nuevo. El plan
   Starter da ~333 req/día en total y el tick horario con 18 lookups gastaba
   432 él solo (cuota agotada el 2026-09-23). Un 402 aborta los lookups
   restantes del tick; las skins con precio cacheado se evalúan igual.

⚠️ PREVENTIVO PARA EL PLAN LIMITADO. Este orden (rankings → /item una vez al
día) existe solo porque el plan Starter de steamwebapi no da para evaluar cada
hora contra /item. Si se amplía el plan o se cambia a un proveedor con más
capacidad, hay que revisarlo: bajar LOOKUP_MIN_INTERVAL (o quitar `_lookup_due`
y volver a un lookup por skin y tick), bajar CACHED_PRICE_MAX_AGE y ajustar
ALERTS_LOOKUP_CAP al nuevo límite. Ver docs/issues/rendimiento/PERF-09-cuota-steamwebapi.md.

A las que cumplen la condición las marca disparadas ANTES de mandar la push:
si el envío revienta a medias, el siguiente tick no las reenvía. Se sacrifica
un aviso perdido a cambio de no duplicar nunca.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from settings import ALERTS_LOOKUP_CAP, ALERTS_MAX_PER_USER
from steam import price_capture
from steam import price_history_repo
from steam.domain.validators import canonical_price
from steam.errors import QuotaExhausted
from steam.rankings_repo import movers_repo, trending_repo
from notifications import repo as notif_repo
from notifications.service import send_to_tokens
from . import repo

logger = logging.getLogger("uvicorn.error")

MAX_NAME_LEN = 200

# ponytail: los schedule de GitHub que refrescan los rankings se retrasan horas
# (medido: 1–5 h entre ticks), así que un tope estricto dejaría sin precio a
# casi todo. 6 h es el compromiso; bajarlo cuando el market-tick sea puntual.
CACHED_PRICE_MAX_AGE = timedelta(hours=6)
# Una consulta a /item por skin y día como máximo.
LOOKUP_MIN_INTERVAL = timedelta(hours=24)
# PUSH-10: un tick a la vez. El bucle interno y el POST del workflow comparten
# proceso; sin esto dos evaluaciones solapadas podrían mandar la misma push dos veces.
_tick_lock = asyncio.Lock()


class AlertError(Exception):
    """Base: el router la traduce a HTTP."""


class LimitReached(AlertError):
    pass


class Duplicate(AlertError):
    pass


class UnknownItem(AlertError):
    pass


class PriceUnavailable(AlertError):
    """No hay precio cacheado y la cuota de /item está agotada: reintentar más tarde."""


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


async def _cached_prices(names: list[str]) -> dict[str, float]:
    """Precio por skin desde las tablas de rankings, ignorando filas viejas.
    movers primero (se reemplaza entero en cada tick); trending como respaldo."""
    cutoff = datetime.now(timezone.utc) - CACHED_PRICE_MAX_AGE
    prices: dict[str, float] = {}
    for ranking in (movers_repo, trending_repo):
        try:
            rows = await ranking.fetch_prices(names)
        except Exception as exc:  # noqa: BLE001 — sin caché se cae al lookup
            logger.warning("[alerts] lectura de %s falló: %s", ranking._table, exc)
            continue
        for row in rows:
            ts = _parse_ts(row.get("updated_at"))
            try:
                price = float(row.get("price_latest") or 0)
            except (TypeError, ValueError):
                price = 0
            if price > 0 and ts is not None and ts >= cutoff:
                prices.setdefault(row["name"], price)
    return prices


def _lookup_due(alerts: list[dict], name: str) -> bool:
    """True si alguna alerta de esa skin no se ha evaluado en las últimas 24 h."""
    cutoff = datetime.now(timezone.utc) - LOOKUP_MIN_INTERVAL
    for a in alerts:
        if a["market_hash_name"] != name:
            continue
        last = _parse_ts(a.get("last_checked_at"))
        if last is None or last < cutoff:
            return True
    return False


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
    # los rankings la resuelven gratis y, si tampoco, un lookup (1 req de cuota,
    # solo en creación). En cualquier caso se registra en tracked_skins para
    # que entre en la captura diaria.
    if not await price_history_repo.is_tracked(market_hash_name):
        price = (await _cached_prices([market_hash_name])).get(market_hash_name)
        if price is None:
            try:
                item = await price_capture._lookup_item(http_client, market_hash_name)
            except QuotaExhausted:
                raise PriceUnavailable("Precio no disponible ahora mismo, inténtalo más tarde")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[alerts] lookup falló para %r: %s", market_hash_name, exc)
                item = {}
            price = canonical_price(item)
        if price is None:
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
    async with _tick_lock:
        return await _evaluate_alerts(http_client)


async def run_tick_loop(http_client: httpx.AsyncClient, interval: float) -> None:
    """Tick interno (PUSH-10): el schedule de GitHub ejecuta el cron «horario» cada
    3–9 h (medido 9 días), así que el backend evalúa él mismo cada `interval` s
    mientras el pinger (PERF-07) lo mantiene despierto. Un fallo se registra y el
    bucle sigue; se para cancelando la tarea (CancelledError no se captura)."""
    while True:
        await asyncio.sleep(interval)
        try:
            await evaluate_alerts(http_client)
        except Exception as exc:  # noqa: BLE001 — el siguiente tick lo reintenta
            logger.error("[alerts] tick interno falló: %s", exc)


async def _evaluate_alerts(http_client: httpx.AsyncClient) -> dict:
    total_active = await repo.count_active()
    alerts = await repo.fetch_active(ALERTS_LOOKUP_CAP)

    names = list(dict.fromkeys(a["market_hash_name"] for a in alerts))
    prices = await _cached_prices(names)
    errors = 0
    quota_exhausted = False
    for name in names:
        if name in prices or not _lookup_due(alerts, name):
            continue
        try:
            item = await price_capture._lookup_item(http_client, name)
        except QuotaExhausted as exc:
            errors += 1
            quota_exhausted = True
            logger.error("[alerts] cuota de steamwebapi agotada, sin más lookups este tick: %s", exc)
            break
        except Exception as exc:  # noqa: BLE001 — best-effort: un fallo no aborta el lote
            errors += 1
            logger.warning("[alerts] lookup falló para %r: %s", name, exc)
            continue
        price = canonical_price(item)
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
        "quota_exhausted": quota_exhausted,
    }
    logger.info("[alerts] %s", out)
    return out
