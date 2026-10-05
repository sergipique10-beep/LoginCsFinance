"""Histórico de precios y precios por mercado (CLEAN-11): el histórico de CSFloat con
sus deltas (`enrich_prices`) y el lookup de CSFloat/Buff (`enrich_market_prices`).
"""
import asyncio
import logging
import time
from datetime import date, timedelta
from functools import partial

import httpx

from steam.cache.history_cache import _item_history_cache
from steam.cache.market_cache import _market_lookup_cache
from steam.adapters import buff_adapter, csfloat_adapter
from steam.adapters.steam_adapter import adapt_legacy_history, adapt_price_rows
from steam.api import MARKET_CLIENTS, csfloat_client, steam_client
from steam.api.steam_client import _history_limiter
from steam.domain import catalog as domain_catalog
from steam.errors.handling import DEGRADABLE, degraded, reason_of
from steam.domain.models import Fetched, HistoryPoint
from steam.errors import HistoryBusy, QuotaExhausted, RateLimited, UnexpectedPayload
from steam.mappers.item_mapper import _delta_from_history

# El adapter del histórico de cada mercado con histórico (`HISTORY_MARKETS`).
_HISTORY_ADAPTERS = {"csfloat": csfloat_adapter.adapt_history, "buff": buff_adapter.adapt_history}

logger = logging.getLogger("uvicorn.error")


async def fetch_history_for_item(
    client: httpx.AsyncClient, name: str, *, limiter_timeout: float | None = None,
) -> Fetched[list[HistoryPoint]]:
    """Histórico de 35 días de CSFloat para el enriquecimiento, la predicción y el chat.

    `ok` con los puntos; `error` con `[]` (y su motivo) si la fuente falló o el cuerpo
    no era una lista: ese vacío se cachea `HISTORY_EMPTY_TTL` (5 min), no 23 h. Si el
    limiter no da hueco en `limiter_timeout`, `HistoryBusy` sin cachear nada (PERF-03).
    """
    cache_key = f"{name}:csfloat:35d"
    now = time.monotonic()
    # Un vacío (fallo o sin datos) vale 5 min, no 23 h: evita tormentas de reintentos
    # sin dejar fuera durante un día una skin que vuelve a tener histórico.
    hit = _item_history_cache.fresh(cache_key, now, empty_ttl=_item_history_cache.empty_ttl)
    if hit is not None:
        return Fetched(hit)
    if limiter_timeout is None:
        await _history_limiter.acquire()
    else:
        try:
            await asyncio.wait_for(_history_limiter.acquire(), timeout=limiter_timeout)
        except asyncio.TimeoutError:
            # Sin cachear: un vacío "por saturación" no es un dato.
            raise HistoryBusy(name) from None
    now = time.monotonic()  # limiter may have blocked; refresh for cache stamps
    today = date.today()
    try:
        raw = await csfloat_client.history(
            client, name, (today - timedelta(days=35)).isoformat(), today.isoformat(), timeout=30.0,
        )
        pts = csfloat_adapter.adapt_history(raw)   # cuerpo no lista → UnexpectedPayload
    except DEGRADABLE as exc:
        logger.warning("[item-history] %s → %s: %s", name, reason_of(exc), exc)
        _item_history_cache.put(cache_key, [], now)
        return degraded("history", reason_of(exc), "empty", [])
    logger.info("[item-history] %s → %d points (csfloat)", name, len(pts))
    _item_history_cache.put(cache_key, pts, now)
    return Fetched(pts)


async def enrich_prices(
    client: httpx.AsyncClient, items: list, concurrency: int = 5, *, limiter_timeout: float | None = None,
) -> list:
    """Deltas 24h/7d/30d desde el histórico de CSFloat. **No muta** la entrada:
    devuelve una lista nueva con dicts nuevos para los items con histórico, y el
    mismo dict, intacto, para los que no tienen (/internal/enrich-tick lo usa para
    distinguir "sin datos" de "con deltas").
    """
    sem = asyncio.Semaphore(concurrency)

    async def fetch(name: str):
        async with sem:
            return await fetch_history_for_item(client, name, limiter_timeout=limiter_timeout)

    histories = await asyncio.gather(*[fetch(it["name"]) for it in items])
    result = []
    for item, fetched in zip(items, histories, strict=True):
        pts = fetched.data
        if pts:
            # Use the most recent point in the CSFloat history as "current" price so
            # we compare CSFloat vs CSFloat (same market). Using priceLatest (Steam)
            # vs CSFloat history produces misleading cross-market deltas.
            latest = pts[-1]["price"]
            item = {
                **item,
                "priceDelta24h": _delta_from_history(pts, 1, latest),
                "priceDelta7d":  _delta_from_history(pts, 7, latest),
                "priceDelta30d": _delta_from_history(pts, 30, latest),
            }
        result.append(item)
    return result


# PERF-17: tras un fallo, no se reintenta durante LOOKUP_FAIL_TTL (5 min). Sin esto,
# cada inventario/movers/búsqueda con la fuente caída repetía dos lookups condenados
# a fallar, gastando cuota y hasta 30 s de timeout. El backoff vive en la propia caché
# (`mark_failed`), aparte del último dato bueno.
def _lookup_stale(market: str, reason: str) -> Fetched[dict[str, float]]:
    """El último lookup bueno, o `{}` (precios a null); deja la línea de degradación."""
    stale = _market_lookup_cache.stale(market)
    if stale is not None:
        return degraded("market_lookup", reason, "stale", stale)
    return degraded("market_lookup", reason, "empty", {})


async def _fetch_market_price_lookup(client: httpx.AsyncClient, market: str) -> Fetched[dict[str, float]]:
    now = time.monotonic()
    hit = _market_lookup_cache.fresh(market, now)
    if hit is not None:
        return Fetched(hit)
    if _market_lookup_cache.in_backoff(market, now):
        return _lookup_stale(market, "backoff")
    try:
        data = await MARKET_CLIENTS[market].prices(client, {"format": "json"}, timeout=30.0)
        rows = adapt_price_rows(data, market=market)   # forma rara → UnexpectedPayload
    except DEGRADABLE as exc:
        logger.warning("[market-lookup] %s failed: %s", market, reason_of(exc))
        _market_lookup_cache.mark_failed(market, now)
        return _lookup_stale(market, reason_of(exc))
    lookup: dict[str, float] = {r.name: r.price for r in rows}
    _market_lookup_cache.put(market, lookup, now)
    logger.info("[market-lookup] %s: %d prices loaded", market, len(lookup))
    return Fetched(lookup)


async def enrich_market_prices(client: httpx.AsyncClient, items: list) -> list:
    """Añade `<market>Price` por cada mercado de domain_catalog.TRACKED_MARKETS. **Muta** los
    items en sitio y devuelve la misma lista."""
    markets = domain_catalog.TRACKED_MARKETS
    lookups = await asyncio.gather(*(_fetch_market_price_lookup(client, m) for m in markets))
    for item in items:
        name = item.get("name", "")
        for market, lookup in zip(markets, lookups, strict=True):
            item[f"{market}Price"] = lookup.data.get(name) or None   # csfloatPrice, buffPrice
    return items


async def get_item_history(client: httpx.AsyncClient, name: str, interval: str, market: str | None,
                           days: int, *, limiter_timeout: float) -> Fetched[list[HistoryPoint]]:
    """GET /item/history: histórico de Steam (ruta legacy) o de Buff/CSFloat.

    Pasa por `_history_limiter` con espera máxima `limiter_timeout` (SEC-16). Ventana
    llena, 429 o 402 → la caché caducada si la hay; si no, sube `HistoryBusy` /
    `RateLimited` / `QuotaExhausted` y la ruta responde 503 con su `code` (CAL-14: el 402
    daba un `200 []` que el front pintaba como «sin histórico»). Un cuerpo ilegible da
    `[]` sin cachear.
    """
    market = market.lower() if market else None
    days = max(1, min(days, 365))  # el frontend pide por timeframe; acotar el rango
    cache_key = f"{name}:{interval}:{market or 'steam'}:{days}"
    now = time.monotonic()
    hit = _item_history_cache.fresh(cache_key, now)
    if hit is not None:
        return Fetched(hit)

    # Buff163/CSFloat usan el endpoint por-market (fechas + quantity); Steam usa la
    # ruta legacy (interval + sold). Distintos hosts, params y forma de respuesta.
    if market in domain_catalog.HISTORY_MARKETS:
        today = date.today()
        fetch = partial(
            MARKET_CLIENTS[market].history,
            client, name, (today - timedelta(days=days)).isoformat(), today.isoformat(),
        )
        adapt = _HISTORY_ADAPTERS[market]
    else:
        fetch = partial(steam_client.legacy_history, client, name, interval)
        adapt = adapt_legacy_history

    try:
        await asyncio.wait_for(_history_limiter.acquire(), timeout=limiter_timeout)
    except asyncio.TimeoutError:
        stale = _item_history_cache.stale(cache_key)
        if stale is not None:
            return degraded("item_history", "rate_limit", "stale", stale)
        raise HistoryBusy(name) from None

    try:
        data = await fetch()
    except (QuotaExhausted, RateLimited) as exc:
        logger.warning("[item-history] steamwebapi %s for %s (%s)", exc.status, name, market or "steam")
        stale = _item_history_cache.stale(cache_key)
        if stale is not None:
            return degraded("item_history", reason_of(exc), "stale", stale)
        raise

    try:
        points: list[HistoryPoint] = adapt(data)
    except UnexpectedPayload:
        # Sin cachear (CAL-14: antes un `[]` por cuerpo ilegible se guardaba 23 h).
        return degraded("item_history", "unexpected_format", "empty", [])
    _item_history_cache.put(cache_key, points, now)
    return Fetched(points)
