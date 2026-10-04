"""Mapper de `/info/markets` → la lista de `MarketProvider` de GET /market/providers."""
from collections.abc import Sequence

from steam.domain import catalog as domain_catalog
from steam.domain.models import MarketProvider, ProviderInfo


def _build_providers(markets: Sequence[ProviderInfo]) -> list[MarketProvider]:
    """Steam primero, luego csfloat y buff: con los datos de la API si vienen, con el
    respaldo estático del dominio si no. Los mercados que no son proveedores
    (`PROVIDER_IDS`) se ignoran."""
    lookup: dict[str, MarketProvider] = {
        m.id: {
            "id": m.id,
            "name": m.name or m.id.capitalize(),
            "logoUrl": m.logo or domain_catalog.KNOWN_LOGOS.get(m.id, ""),
        }
        for m in markets if m.id in domain_catalog.PROVIDER_IDS
    }
    providers: list[MarketProvider] = [{"id": "steam", "name": "Steam", "logoUrl": domain_catalog.STEAM_FAVICON}]
    for pid in ("csfloat", "buff"):
        providers.append(lookup.get(pid) or domain_catalog.fallback_provider(pid))
    return providers
