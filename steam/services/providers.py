"""Mercados soportados como price provider (GET /market/providers), con respaldo
estático si steamwebapi no responde (PERF-17) (CLEAN-11)."""
import logging
import time

import httpx

from stores import _market_providers_cache
from steam.clients import steamwebapi
from steam.domain import catalog as domain_catalog
from steam.domain.models import MarketProvider
from steam.errors import SourceTimeout, SourceUnavailable, UpstreamError

logger = logging.getLogger("uvicorn.error")


def _providers_stale() -> list[MarketProvider]:
    """PERF-17: con la fuente caída, el último dato bueno si existe; si no, el respaldo."""
    last = _market_providers_cache.stale("providers")
    return last if last is not None else domain_catalog.fallback_providers()


async def fetch_market_providers(client: httpx.AsyncClient) -> list[MarketProvider]:
    now = time.monotonic()
    hit = _market_providers_cache.fresh("providers", now)
    if hit is not None:
        return hit
    if _market_providers_cache.in_backoff("providers", now):
        return _providers_stale()
    try:
        try:
            data = await steamwebapi.info_markets(client)
        except (SourceTimeout, SourceUnavailable):
            raise
        except UpstreamError as exc:
            logger.warning("[market-providers] info/markets returned %s", exc.status)
            _market_providers_cache.mark_failed("providers", now)
            return _providers_stale()
        if not isinstance(data, list):
            _market_providers_cache.mark_failed("providers", now)
            return _providers_stale()


        if data:
            logger.info("[market-providers] sample keys: %s", list(data[0].keys()))


        lookup: dict[str, MarketProvider] = {}
        for m in data:
            mid = (m.get("id") or m.get("key") or m.get("name") or "").lower()
            if mid in domain_catalog.PROVIDER_IDS:
                api_logo = (
                    m.get("logo") or m.get("logoUrl") or m.get("logo_url") or
                    m.get("image") or m.get("imageUrl") or m.get("image_url") or
                    m.get("icon") or m.get("iconUrl") or m.get("icon_url") or
                    m.get("thumbnail") or ""
                )
                lookup[mid] = {
                    "id":      mid,
                    "name":    m.get("name") or mid.capitalize(),
                    "logoUrl": api_logo or domain_catalog.KNOWN_LOGOS.get(mid, ""),
                }


        providers: list[MarketProvider] = [{"id": "steam", "name": "Steam", "logoUrl": domain_catalog.STEAM_FAVICON}]
        for pid in ("csfloat", "buff"):
            providers.append(lookup.get(pid) or domain_catalog.fallback_provider(pid))


        _market_providers_cache.put("providers", providers, now)
        logger.info("[market-providers] loaded %d providers", len(providers))
        return providers
    except Exception as exc:
        logger.warning("[market-providers] failed: %s", exc)
        _market_providers_cache.mark_failed("providers", now)
        return _providers_stale()
