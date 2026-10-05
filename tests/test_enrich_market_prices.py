"""CLEAN-04: _enrich_market_prices recorre catalog.TRACKED_MARKETS (un lookup y un campo
`<market>Price` por mercado) en vez de repetir los nombres a mano."""
from unittest.mock import AsyncMock

from steam.domain.models import Fetched
from steam.services import pricing
from steam.domain import catalog


async def test_one_lookup_and_one_field_per_tracked_market(monkeypatch):
    lookups = {"csfloat": {"AK": 10.0}, "buff": {"AK": 9.0}, "nuevo": {"AK": 8.0}}
    fetch = AsyncMock(side_effect=lambda client, market: Fetched(lookups[market]))
    monkeypatch.setattr(pricing, "_fetch_market_price_lookup", fetch)
    monkeypatch.setattr(catalog, "TRACKED_MARKETS", ("csfloat", "buff", "nuevo"))

    items = await pricing.enrich_market_prices(object(), [{"name": "AK"}, {"name": "M4"}])

    assert sorted(c.args[1] for c in fetch.await_args_list) == ["buff", "csfloat", "nuevo"]
    assert items[0] == {"name": "AK", "csfloatPrice": 10.0, "buffPrice": 9.0, "nuevoPrice": 8.0}
    assert items[1] == {"name": "M4", "csfloatPrice": None, "buffPrice": None, "nuevoPrice": None}
