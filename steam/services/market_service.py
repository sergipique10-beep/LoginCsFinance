"""Orquestación del mercado (CLEAN-11, partido en CLEAN-18): búsqueda, item completo,
índice de mercado y precios por mercado. Los rankings y sus ticks están en
`rankings_service.py`; el histórico del índice en `cap_history_service.py`. Sin FastAPI:
lanza los errores de `steam/errors/` y las rutas los traducen a HTTP.
"""
import logging
import time
from typing import Any, TypeVar

import httpx

from steam.cache.history_cache import _topmovers_raw_cache
from steam.cache.market_cache import (
    _item_price_cache, _market_index_cache, _market_prices_cache, _search_cache,
)
from steam.adapters.steam_adapter import adapt_items, adapt_market_index
from steam.api import steam_client
from steam.errors.handling import degraded, log_degraded
from steam.domain.models import Fetched, SkinCard
from steam.domain.normalizers import names_match
from steam.domain.rules import (
    is_sticker_slab,
)
from steam.errors import (
    QuotaExhausted, SourceTimeout, SourceUnavailable, UpstreamError,
)
from steam.mappers.item_mapper import _map_item
from steam.mappers.market_index_mapper import _map_market_index_point
from steam.services import catalog_service, pricing_service

from steam.services.rankings_service import _MOVERS_SELECT
from steam.utils.strings import lower_key

logger = logging.getLogger("uvicorn.error")

_SEARCH_LIMIT = 30

_T = TypeVar("_T")


# ── Fuentes de los rankings ───────────────────────────────────────────────────

async def search_items(client: httpx.AsyncClient, query: str, *, max: int, select: str) -> Any:
    """La búsqueda en /items, una sola implementación para /market/items, /market/price
    y las dos tools del chat. Cada llamador conserva su `max`, su `select` y qué hace
    con un cuerpo que no es lista (CAL-14 decidirá si se unifican)."""
    return await steam_client.items(client, search=query, max=max, select=select)


def search_cache_key(namespace: str, query: str) -> str:
    """Clave de `_search_cache` (CAL-11). La búsqueda web (`market`) y la del chat
    (`chat`) piden `select` distintos: el del chat no trae los campos del Liquidity
    Score, así que compartir clave dejaba a /market/items sirviendo hasta 10 items sin
    liquidez durante 5 min."""
    return f"{namespace}:{lower_key(query)}"


def _stale_or_raise(flow: str, stale: _T | None, exc: QuotaExhausted) -> Fetched[_T]:
    """SEC-16 — 402 de steamwebapi: mejor un dato caducado que un error, porque la
    cuota no vuelve hasta el día 10. Sin caché, el 402 sube y la ruta da 503."""
    if stale is not None:
        return degraded(flow, "quota", "stale", stale)
    raise exc


async def search_market(client: httpx.AsyncClient, query: str) -> Fetched[list[SkinCard]]:
    """GET /market/items: búsqueda por nombre con el shape completo y caché de 5 min."""
    cache_key = search_cache_key("market", query)
    now = time.monotonic()
    hit = _search_cache.fresh(cache_key, now)
    if hit is not None:
        return Fetched(hit)

    try:
        data = await search_items(client, query, max=_SEARCH_LIMIT, select=_MOVERS_SELECT)
    except QuotaExhausted as exc:
        return _stale_or_raise("search", _search_cache.stale(cache_key), exc)
    items = adapt_items(data)   # cuerpo no lista → UnexpectedPayload

    catalog_service.cache_images(items)
    result = [
        _map_item(item) for item in items
        if (item.price_latest_sell or 0) > 0 and not is_sticker_slab(item)
    ][:_SEARCH_LIMIT]

    await catalog_service.fetch_static_images(client)
    result = await pricing_service.enrich_market_prices(client, result)
    catalog_service.enrich_images_from_cache(result)

    _search_cache.put(cache_key, result, now)
    logger.info("[market-items] q=%r → %d results", query, len(result))
    return Fetched(result)


async def get_item_full(client: httpx.AsyncClient, query: str) -> Fetched[SkinCard | None]:
    """GET /market/price: un item con el shape completo de _map_item (liquidez y
    volumen incluidos), o None si la búsqueda no trae el nombre exacto."""
    cache_key = lower_key(query)
    now = time.monotonic()
    hit = _item_price_cache.fresh(cache_key, now)
    if hit is not None:
        return Fetched(hit)

    try:
        data = await search_items(client, query, max=_SEARCH_LIMIT, select=_MOVERS_SELECT)
    except QuotaExhausted as exc:
        return _stale_or_raise("item_price", _item_price_cache.stale(cache_key), exc)
    items = adapt_items(data)   # cuerpo no lista → UnexpectedPayload

    # steamwebapi /items?search es fuzzy: nos quedamos con el match exacto por
    # markethashname (el nombre canónico en inglés, el mismo que manda el frontend).
    match = next((i for i in items if names_match(i.name, query)), None)
    if match is None:
        return Fetched(None)

    catalog_service.cache_images([match])
    item = _map_item(match)
    (item,) = await pricing_service.enrich_prices(client, [item])
    (item,) = await pricing_service.enrich_market_prices(client, [item])
    await catalog_service.fetch_static_images(client)
    catalog_service.enrich_images_from_cache([item])

    _item_price_cache.put(cache_key, item, now)
    logger.info("[market-price] name=%r → hit", query)
    return Fetched(item)


async def get_market_index(client: httpx.AsyncClient, tf: str) -> Fetched[dict]:
    cache_key = tf
    now = time.monotonic()
    hit = _market_index_cache.fresh(cache_key, now)
    if hit is not None:
        return Fetched(hit)

    try:
        data = await steam_client.market_index(client)
    except QuotaExhausted as exc:
        logger.warning("[market-index] daily limit reached (402)")
        return _stale_or_raise("market_index", _market_index_cache.stale(cache_key), exc)
    except (SourceTimeout, SourceUnavailable):
        raise
    except UpstreamError as exc:
        logger.error("[market-index] steamwebapi returned %s | body: %s", exc.status, exc.body_excerpt[:500])
        raise

    mi = adapt_market_index(data)   # forma inesperada → UnexpectedPayload
    if mi.dropped_movers:
        log_degraded("market_index", "invalid_field", "fallback")   # gainers sin nombre, descartados
    _topmovers_raw_cache.put("latest", (mi.gainers, mi.losers), now)
    top = mi.gainers[0] if mi.gainers else None

    # UX-39: topmovers no trae la rareza; sale del catálogo estático (23 h, sin cuota).
    rarity = None
    if top:
        await catalog_service.fetch_static_images(client)
        rarity = catalog_service.rarity_from_cache(top.item.name)

    result = {
        "turnover24h": mi.turnover_24h or 0.0,
        "sold24h": mi.sold_24h or 0,
        # UX-35: no es «el más activo» sino el que más ha subido de precio en 24 h
        # (gainers[0]); change24h es ese porcentaje. El nombre del campo se conserva
        # por contrato con el front.
        "hottestItem": {
            "name": top.item.name if top else "—",
            "change24h": (top.change_24h or 0.0) if top else 0.0,
            # UX-38: el precio pone el porcentaje en contexto (+450 % de 0,17 $).
            "price": top.item.price if top else None,
            "rarity": rarity[0] if rarity else None,
            "rarityColor": rarity[1] if rarity else None,
        },
        "history": [_map_market_index_point(p) for p in mi.history],
    }
    _market_index_cache.put(cache_key, result, now)
    return Fetched(result)


async def get_market_prices(client: httpx.AsyncClient, market: str, name: str | None,
                            currency: str | None) -> Fetched[Any]:
    """GET /market/prices: passthrough de /market/{market}/prices, caché de 5 min."""
    cache_key = f"{market}:{lower_key(name)}:{lower_key(currency) or 'usd'}"
    now = time.monotonic()
    hit = _market_prices_cache.fresh(cache_key, now)
    if hit is not None:
        return Fetched(hit)

    params: dict = {}
    if name:
        params["market_hash_name"] = name
    if currency:
        params["currency"] = currency

    try:
        data = await steam_client.market_prices(client, market, params, timeout=15.0)
    except QuotaExhausted as exc:
        return _stale_or_raise("market_prices", _market_prices_cache.stale(cache_key), exc)

    _market_prices_cache.put(cache_key, data, now)
    logger.info("[market-prices] market=%r name=%r → %s items", market, name, len(data) if isinstance(data, list) else "object")
    return Fetched(data)
