"""Histórico de precios y precios por mercado (CLEAN-11): el histórico de CSFloat con
sus deltas (`enrich_prices`) y el lookup de CSFloat/Buff (`enrich_market_prices`).
"""
import asyncio
import logging
import time
from datetime import date, timedelta
from functools import partial

import httpx

from stores import HISTORY_EMPTY_TTL, _item_history_cache, _market_lookup_cache
from steam.api import MARKET_CLIENTS, csfloat_client, steam_client
from steam.api.steam_client import _history_limiter
from steam.domain import catalog as domain_catalog
from steam.errors.handling import log_degraded, reason_of
from steam.domain.models import Fetched, HistoryPoint
from steam.domain.validators import has_price
from steam.errors import (
    HistoryBusy, QuotaExhausted, RateLimited, SourceTimeout, SourceUnavailable, UpstreamError,
)
from steam.mappers.items import _delta_from_history

logger = logging.getLogger("uvicorn.error")


async def fetch_history_for_item(
    client: httpx.AsyncClient, name: str, *, limiter_timeout: float | None = None,
) -> list:
    cache_key = f"{name}:csfloat:35d"
    now = time.monotonic()
    # Un vacío (fallo o sin datos) vale 5 min, no 23 h: evita tormentas de reintentos
    # sin dejar fuera durante un día una skin que vuelve a tener histórico.
    hit = _item_history_cache.fresh(cache_key, now, empty_ttl=HISTORY_EMPTY_TTL)
    if hit is not None:
        return hit
    try:
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
        except (SourceTimeout, SourceUnavailable):
            raise   # red: lo registra el except genérico de abajo, como antes
        except UpstreamError as exc:
            logger.warning("[item-history] %s → HTTP %s: %s", name, exc.status, exc.body_excerpt[:200])
            log_degraded("history", reason_of(exc), "empty")
            _item_history_cache.put(cache_key, [], now)
            return []
        if not isinstance(raw, list):
            logger.warning("[item-history] %s → unexpected format: %s", name, str(raw)[:200])
            log_degraded("history", "unexpected_format", "empty")
            _item_history_cache.put(cache_key, [], now)
            return []
        pts = sorted(
            [
                {
                    "date":   p.get("createdat", "")[:10],
                    "price":  float(p.get("price") or 0),
                    "volume": int(p.get("quantity") or 0),
                }
                for p in raw if has_price(p)
            ],
            key=lambda p: p["date"],
        )
        logger.info("[item-history] %s → %d points (csfloat)", name, len(pts))
        _item_history_cache.put(cache_key, pts, now)
        return pts
    except HistoryBusy:
        raise
    except Exception as exc:
        logger.warning("[item-history] %s → exception: %s", name, exc)
        log_degraded("history", reason_of(exc), "empty")
        _item_history_cache.put(cache_key, [], now)
        return []


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
    for item, pts in zip(items, histories, strict=True):
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
def _lookup_stale(market: str, reason: str) -> dict[str, float]:
    """El último lookup bueno, o `{}` (precios a null); deja la línea de degradación."""
    stale = _market_lookup_cache.stale(market)
    log_degraded("market_lookup", reason, "stale" if stale is not None else "empty")
    return stale if stale is not None else {}


async def _fetch_market_price_lookup(client: httpx.AsyncClient, market: str) -> dict[str, float]:
    now = time.monotonic()
    hit = _market_lookup_cache.fresh(market, now)
    if hit is not None:
        return hit
    if _market_lookup_cache.in_backoff(market, now):
        return _lookup_stale(market, "backoff")
    try:
        try:
            data = await MARKET_CLIENTS[market].prices(client, {"format": "json"}, timeout=30.0)
        except (SourceTimeout, SourceUnavailable):
            raise
        except UpstreamError as exc:
            logger.warning("[market-lookup] %s returned %s", market, exc.status)
            _market_lookup_cache.mark_failed(market, now)
            return _lookup_stale(market, reason_of(exc))
        if not isinstance(data, list):
            _market_lookup_cache.mark_failed(market, now)
            return _lookup_stale(market, "unexpected_format")
        lookup: dict[str, float] = {}
        for item in data:
            name = item.get("market_hash_name") or item.get("markethashname") or item.get("name")
            price = item.get("price") or item.get("value") or 0
            if name and price:
                lookup[name] = float(price)
        _market_lookup_cache.put(market, lookup, now)
        logger.info("[market-lookup] %s: %d prices loaded", market, len(lookup))
        return lookup
    except Exception as exc:
        logger.warning("[market-lookup] could not fetch %s: %s", market, exc)
        _market_lookup_cache.mark_failed(market, now)
        return _lookup_stale(market, reason_of(exc))


async def enrich_market_prices(client: httpx.AsyncClient, items: list) -> list:
    """Añade `<market>Price` por cada mercado de domain_catalog.TRACKED_MARKETS. **Muta** los
    items en sitio y devuelve la misma lista."""
    markets = domain_catalog.TRACKED_MARKETS
    lookups = await asyncio.gather(*(_fetch_market_price_lookup(client, m) for m in markets))
    for item in items:
        name = item.get("name", "")
        for market, lookup in zip(markets, lookups, strict=True):
            item[f"{market}Price"] = lookup.get(name) or None   # csfloatPrice, buffPrice
    return items


async def get_item_history(client: httpx.AsyncClient, name: str, interval: str, market: str | None,
                           days: int, *, limiter_timeout: float) -> Fetched[list[HistoryPoint]]:
    """GET /item/history: histórico de Steam (ruta legacy) o de Buff/CSFloat.

    Pasa por `_history_limiter` con espera máxima `limiter_timeout` (SEC-16). Ventana
    llena o 429 → la caché caducada si la hay; si no, `HistoryBusy` / `RateLimited`.
    Un 402 da `[]` sin cachear (CAL-14).
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
        volume_key = "quantity"
    else:
        fetch = partial(steam_client.legacy_history, client, name, interval)
        volume_key = "sold"

    try:
        await asyncio.wait_for(_history_limiter.acquire(), timeout=limiter_timeout)
    except asyncio.TimeoutError:
        stale = _item_history_cache.stale(cache_key)
        if stale is not None:
            log_degraded("item_history", "rate_limit", "stale")
            return Fetched(stale, "stale", "rate_limit")
        raise HistoryBusy(name) from None

    try:
        data = await fetch()
    except QuotaExhausted:
        logger.warning("[item-history] daily limit reached for %s (%s)", name, market or "steam")
        log_degraded("item_history", "quota", "empty")
        return Fetched([], "error", "quota")
    except RateLimited:
        logger.warning("[item-history] steamwebapi 429 for %s (%s)", name, market or "steam")
        stale = _item_history_cache.stale(cache_key)
        if stale is not None:
            log_degraded("item_history", "rate_limit", "stale")
            return Fetched(stale, "stale", "rate_limit")
        raise

    if not isinstance(data, list):
        log_degraded("item_history", "unexpected_format", "empty")
    raw = data if isinstance(data, list) else []
    points: list[HistoryPoint] = sorted(
        [
            {
                "date":   p.get("createdat", "")[:10],
                "price":  float(p.get("price") or 0),
                "volume": int(p.get(volume_key) or 0),
            }
            for p in raw if has_price(p)
        ],
        key=lambda p: p["date"],
    )
    _item_history_cache.put(cache_key, points, now)
    if not isinstance(data, list):
        return Fetched(points, "error", "unexpected_format")   # CAL-14: y se cachea 23 h
    return Fetched(points)
