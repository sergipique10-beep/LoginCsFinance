"""Mercados soportados como price provider (GET /market/providers), con respaldo
estático si steamwebapi no responde (PERF-17) (CLEAN-11)."""
import logging
import time

import httpx

from stores import _market_providers_cache
from steam.adapters.provider_adapter import adapt_markets
from steam.api import steam_client
from steam.domain import catalog as domain_catalog
from steam.errors.handling import log_degraded, reason_of
from steam.domain.models import Fetched, MarketProvider
from steam.errors import SourceTimeout, SourceUnavailable, UnexpectedPayload, UpstreamError
from steam.mappers.provider_mapper import _build_providers

logger = logging.getLogger("uvicorn.error")


def _providers_stale(reason: str) -> Fetched[list[MarketProvider]]:
    """PERF-17: con la fuente caída, el último dato bueno si existe; si no, el respaldo."""
    last = _market_providers_cache.stale("providers")
    if last is not None:
        log_degraded("providers", reason, "stale")
        return Fetched(last, "stale", reason)
    log_degraded("providers", reason, "fallback")
    return Fetched(domain_catalog.fallback_providers(), "partial", reason)


async def fetch_market_providers(client: httpx.AsyncClient) -> Fetched[list[MarketProvider]]:
    now = time.monotonic()
    hit = _market_providers_cache.fresh("providers", now)
    if hit is not None:
        return Fetched(hit)
    if _market_providers_cache.in_backoff("providers", now):
        return _providers_stale("backoff")
    try:
        try:
            data = await steam_client.info_markets(client)
        except (SourceTimeout, SourceUnavailable):
            raise
        except UpstreamError as exc:
            logger.warning("[market-providers] info/markets returned %s", exc.status)
            _market_providers_cache.mark_failed("providers", now)
            return _providers_stale(reason_of(exc))
        try:
            markets = adapt_markets(data)
        except UnexpectedPayload:
            _market_providers_cache.mark_failed("providers", now)
            return _providers_stale("unexpected_format")
        providers = _build_providers(markets)

        _market_providers_cache.put("providers", providers, now)
        logger.info("[market-providers] loaded %d providers", len(providers))
        return Fetched(providers)
    except Exception as exc:
        logger.warning("[market-providers] failed: %s", exc)
        _market_providers_cache.mark_failed("providers", now)
        return _providers_stale(reason_of(exc))
