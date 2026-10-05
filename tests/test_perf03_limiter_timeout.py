"""PERF-03: el chat no espera al _history_limiter más de CHAT_LIMITER_TIMEOUT;
los crons siguen esperando lo que haga falta."""
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from steam.services import catalog_service, pricing_service
from steam.api.steam_client import _history_limiter
from steam.errors import HistoryBusy
from stores import _item_history_cache
from tools import market_tools


def _fill_window(age: float = 0.0) -> None:
    now = time.monotonic()
    _history_limiter._calls = [now - age] * _history_limiter._limit


@pytest.fixture(autouse=True)
def _clean_limiter():
    _history_limiter._calls = []
    _item_history_cache.clear()
    yield
    _history_limiter._calls = []
    _item_history_cache.clear()


@pytest.mark.asyncio
async def test_chat_path_gives_up_fast_when_window_is_full():
    _fill_window()
    t0 = time.monotonic()

    with pytest.raises(HistoryBusy):
        await pricing_service.fetch_history_for_item(MagicMock(), "X", limiter_timeout=0.2)

    assert time.monotonic() - t0 < 1.5
    assert "X:csfloat:35d" not in _item_history_cache        # no se cachea un vacío falso
    assert len(_history_limiter._calls) == _history_limiter._limit   # sin hueco fantasma


@pytest.mark.asyncio
async def test_cron_path_keeps_waiting_without_timeout():
    _fill_window(age=_history_limiter._window - 0.3)         # la ventana se libera en ~0,3 s
    client = MagicMock()
    client.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: []))
    t0 = time.monotonic()

    pts = await pricing_service.fetch_history_for_item(client, "Y")

    assert pts.data == []
    assert time.monotonic() - t0 >= 0.2                       # esperó, no falló
    client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_historial_tool_explains_saturation_instead_of_waiting(monkeypatch):
    monkeypatch.setattr(pricing_service, "fetch_history_for_item", AsyncMock(side_effect=HistoryBusy("X")))

    out = await market_tools._historial_precio(market_hash_name="X", client=MagicMock())

    assert out == {"error": market_tools.HISTORY_BUSY_MSG}


@pytest.mark.asyncio
async def test_precio_tool_answers_without_deltas_when_busy_and_does_not_cache(monkeypatch):
    from stores import _item_price_cache
    _item_price_cache.clear()
    raw = {"markethashname": "AK", "image": "https://img/ak.png", "pricelatestsell": 10}
    client = MagicMock()
    client.get = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: [raw]))
    monkeypatch.setattr("steam.mappers.item_mapper._map_item", lambda r: {"name": "AK", "priceLatest": 10.0})
    monkeypatch.setattr(pricing_service, "enrich_prices", AsyncMock(side_effect=HistoryBusy("AK")))
    monkeypatch.setattr(pricing_service, "enrich_market_prices", AsyncMock(side_effect=lambda c, items: items))
    monkeypatch.setattr(catalog_service, "fetch_static_images", AsyncMock())
    monkeypatch.setattr(catalog_service, "enrich_images_from_cache", lambda items: None)

    item = await market_tools._consultar_precio_skin(market_hash_name="AK", client=client)

    assert item["priceLatest"] == 10.0
    assert item["aviso"] == market_tools.HISTORY_BUSY_MSG
    assert "ak" not in _item_price_cache                      # sin deltas no se cachea 5 min
