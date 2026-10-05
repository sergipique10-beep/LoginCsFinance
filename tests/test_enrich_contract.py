"""CLEAN-05: las tres funciones de enriquecimiento devuelven la lista. Solo
_enrich_prices deja la entrada intacta; las otras dos la mutan y devuelven la misma."""
import copy
from unittest.mock import AsyncMock

from steam.domain.models import Fetched
from steam.services import catalog, pricing
from stores import _item_image_cache

STUB = {"name": "AK", "priceDelta24h": None, "priceDelta7d": None, "priceDelta30d": None}


async def test_enrich_prices_without_history_returns_stub_intact(monkeypatch):
    # /internal/enrich-tick depende de esto: sin histórico, los tres deltas siguen a
    # None y la fila solo se marca como intentada (no se pisan los de _inline_delta).
    monkeypatch.setattr(pricing, "fetch_history_for_item", AsyncMock(return_value=Fetched([], "error", "http_500")))
    items = [dict(STUB)]

    out = await pricing.enrich_prices(object(), items)

    assert out == [STUB]
    assert items == [STUB]


async def test_enrich_prices_with_history_does_not_mutate_input(monkeypatch):
    pts = [{"date": "2026-09-01", "price": 10.0, "volume": 1}, {"date": "2026-10-04", "price": 12.0, "volume": 1}]
    monkeypatch.setattr(pricing, "fetch_history_for_item", AsyncMock(return_value=Fetched(pts)))
    items = [dict(STUB)]

    out = await pricing.enrich_prices(object(), items)

    assert out[0] is not items[0]
    assert out[0]["priceDelta30d"] is not None
    assert items == [STUB]


async def test_enrich_market_prices_mutates_and_returns_same_list(monkeypatch):
    monkeypatch.setattr(pricing, "_fetch_market_price_lookup", AsyncMock(return_value=Fetched({"AK": 5.0})))
    items = [{"name": "AK"}]

    out = await pricing.enrich_market_prices(object(), items)

    assert out is items
    assert items[0]["csfloatPrice"] == 5.0


def test_enrich_images_from_cache_mutates_and_returns_same_list(monkeypatch):
    monkeypatch.setitem(_item_image_cache, "AK", "https://img/ak.png")
    items = [{"name": "AK"}]
    before = copy.deepcopy(items)

    out = catalog.enrich_images_from_cache(items)

    assert out is items
    assert items != before and items[0]["image"] == "https://img/ak.png"


def test_enrich_images_from_cache_returns_list_with_empty_cache(monkeypatch):
    monkeypatch.setattr(catalog, "_item_image_cache", {})
    items = [{"name": "AK"}]

    assert catalog.enrich_images_from_cache(items) is items
