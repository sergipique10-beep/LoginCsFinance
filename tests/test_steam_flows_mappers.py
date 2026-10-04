"""CAL-09 (Fase 0): flujos de los mappers puros de steam/mappers/.

Fijan el comportamiento ACTUAL ante payload normal, incompleto e implausible, para que el
refactor (CLEAN-08) pudiera partir el módulo sin cambiar nada.
"""
from datetime import date, timedelta

import httpx
import pytest

from steam.adapters.news_adapter import adapt_news_entry
from steam.adapters.steam_adapter import adapt_inventory, adapt_item, adapt_items, adapt_market_index
from steam.api.news_client import fetch_og_image
from steam.domain.catalog import WEAPON_CATEGORY, weapon_category
from steam.errors import SourceUnavailable, UpstreamError
from steam.mappers.item_mapper import (
    _STEAM_CDN, _delta_from_history, _normalize_image, _resolve_phase, _safe_delta,
)
from steam.mappers.market_index_mapper import _map_market_index_point
from steam.mappers.news_mapper import _map_news_item, is_readable_news


def _d(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


@pytest.mark.parametrize("raw, expected", [
    ("", ""),
    ("https://cdn/x.png", "https://cdn/x.png"),
    ("/economy/image/abc", f"{_STEAM_CDN}/economy/image/abc"),
    ("abc123", f"{_STEAM_CDN}/economy/image/abc123"),
])
def test_normalize_image(raw, expected):
    assert _normalize_image(raw) == expected


@pytest.mark.parametrize("pts, days, latest, expected", [
    ([], 7, 10.0, None),                                          # sin histórico
    ([{"date": _d(10), "price": 8.0}], 7, 0, None),               # sin precio actual
    ([{"date": _d(1), "price": 8.0}], 7, 10.0, None),             # nada tan antiguo
    ([{"date": _d(10), "price": 0}], 7, 10.0, None),              # referencia a cero
    ([{"date": _d(10), "price": 8.0}, {"date": _d(8), "price": 5.0}], 7, 10.0, 100.0),  # el más reciente ≤ corte
])
def test_delta_from_history(pts, days, latest, expected):
    assert _delta_from_history(pts, days, latest) == expected


@pytest.mark.parametrize("new, old, expected", [(None, 1, None), (1, 0, None), (12, 10, 20.0)])
def test_safe_delta(new, old, expected):
    assert _safe_delta(new, old) == expected


@pytest.mark.parametrize("item, expected", [
    ({}, None),
    ({"paintindex": 418}, None),
    ({"paintindex": 418, "variants": [{"paintindex": 418, "phase": "Phase 1"}]}, "Phase 1"),
    ({"paintindex": 418, "variants": [{"paintindex": 419, "phase": "Phase 2"}]}, None),
])
def test_resolve_phase(item, expected):
    assert _resolve_phase(adapt_item(item)) == expected


def test_weapon_category_exact_and_fallbacks():
    key, cat = next(iter(WEAPON_CATEGORY.items()))
    assert weapon_category(key.upper()) == cat           # normaliza mayúsculas
    assert weapon_category(None) is None
    assert weapon_category("Sport Gloves") == "Gloves"
    assert weapon_category("shadow daggers") == "Knife"
    assert weapon_category("sticker capsule") == "Sticker"
    assert weapon_category("special agent") == "Agent"
    assert weapon_category("music kit") == "Music Kit"   # último recurso: title()


def test_market_index_point_normal_e_incompleto():
    mi = adapt_market_index([{"ts": "2026-10-01", "value": "3.5", "change": 1, "volume": "7"}, {}])
    assert _map_market_index_point(mi.history[0]) == {
        "date": "2026-10-01", "price": 3.5, "change": 1.0, "volume": 7,
    }
    assert _map_market_index_point(mi.history[1]) == {"date": "", "price": 0.0, "change": 0.0, "volume": 0}


@pytest.mark.parametrize("title, expected", [
    ("", True),
    ("123 !!!", True),
    ("Major Copenhagen: results", True),
    ("Новости турнира", False),
    ("CS2 大会 结果", False),
])
def test_is_readable_news(title, expected):
    assert is_readable_news(adapt_news_entry({"title": title})) is expected


@pytest.mark.parametrize("feedname, color", [
    ("Valve Blog", "4a9eff"), ("hltv.org", "8847ff"), ("PC Gamer", "f0c040"),
])
def test_map_news_item_colores(feedname, color):
    assert _map_news_item(adapt_news_entry({"feedname": feedname, "date": 0}), 0)["categoryColor"] == color


def test_map_news_item_incompleto():
    out = _map_news_item(adapt_news_entry({}), 3)
    assert out["id"] == "3"
    assert out["date"] == ""                  # sin fecha → "" (no excepción)
    assert out["source"] == "NEWS"            # sin autor → feedlabel
    assert out["category"] == "NEWS"
    assert out["featured"] is False
    assert _map_news_item(adapt_news_entry({"author": "  Ana  ", "date": 0}), 0)["source"] == "Ana"


def _client(status=200, text="", exc=None):
    def handler(request):
        if exc:
            raise exc
        return httpx.Response(status, text=text)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("text, expected", [
    ('<meta property="og:image" content="https://img/a.jpg">', "https://img/a.jpg"),
    ('<meta content="https://img/b.jpg" property="og:image">', "https://img/b.jpg"),
    ("<html>sin meta</html>", ""),   # la página no trae og:image: no es un error
])
async def test_fetch_og_image(text, expected):
    async with _client(200, text) as client:
        assert await fetch_og_image(client, "https://news/x") == expected


@pytest.mark.parametrize("status, exc, error", [
    (404, None, UpstreamError),
    (200, httpx.ConnectError("caído"), SourceUnavailable),
])
async def test_fetch_og_image_lanza_el_error_tipado(status, exc, error):
    # CLEAN-14: antes devolvía "" en silencio; ahora decide (y registra) services/news.
    async with _client(status, "", exc) as client:
        with pytest.raises(error):
            await fetch_og_image(client, "https://news/x")


# ── CLEAN-13: las fixtures de tests/fixtures/ son payloads válidos para los mappers ──

def test_fixture_items_produce_tarjetas_completas(payload):
    from steam.mappers.item_mapper import _map_item
    from tests.test_steam_contract_market import SKIN_CARD_KEYS
    for item in adapt_items(payload("steamwebapi/items")):
        assert set(_map_item(item)) == SKIN_CARD_KEYS


def test_fixture_inventory_anidado_y_plano(payload):
    from steam.mappers.item_mapper import _map_item
    nested, flat = adapt_inventory(payload("steamwebapi/inventory"))
    assert _map_item(nested)["floatValue"] == 0.2345
    assert _map_item(flat)["name"] == "Solitude (Field-Tested)"   # markethashname gana a marketname
