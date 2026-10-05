"""Mercados soportados como price provider (GET /market/providers), con respaldo
estático si steamwebapi no responde (PERF-17) (CLEAN-11)."""
import logging
import time

import httpx

from stores import _market_providers_cache
from steam.adapters.provider_adapter import adapt_markets
from steam.api import steam_client
from steam.domain import catalog as domain_catalog
from steam.errors.handling import DEGRADABLE, degraded, reason_of
from steam.domain.models import Fetched, MarketProvider
from steam.mappers.provider_mapper import _build_providers

logger = logging.getLogger("uvicorn.error")


def _providers_stale(reason: str) -> Fetched[list[MarketProvider]]:
    """PERF-17: con la fuente caída, el último dato bueno si existe; si no, el respaldo."""
    last = _market_providers_cache.stale("providers")
    if last is not None:
        return degraded("providers", reason, "stale", last)
    return degraded("providers", reason, "fallback", domain_catalog.fallback_providers())


async def fetch_market_providers(client: httpx.AsyncClient) -> Fetched[list[MarketProvider]]:
    now = time.monotonic()
    hit = _market_providers_cache.fresh("providers", now)
    if hit is not None:
        return Fetched(hit)
    if _market_providers_cache.in_backoff("providers", now):
        return _providers_stale("backoff")
    try:
        markets = adapt_markets(await steam_client.info_markets(client))   # forma rara → UnexpectedPayload
    except DEGRADABLE as exc:
        logger.warning("[market-providers] info/markets failed: %s", reason_of(exc))
        _market_providers_cache.mark_failed("providers", now)
        return _providers_stale(reason_of(exc))
    providers = _build_providers(markets)
    _market_providers_cache.put("providers", providers, now)
    logger.info("[market-providers] loaded %d providers", len(providers))
    return Fetched(providers)
