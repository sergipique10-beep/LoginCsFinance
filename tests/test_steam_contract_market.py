"""CAL-09 (Fase 0): contrato de las lecturas de /market/* con steamwebapi simulado.

Status y conjunto EXACTO de claves de cada endpoint: es lo que consume el front
(CS-FINANCE-ionic, fuera de este repo). Durante STEAM-REFACTOR estos tests no se tocan;
si uno tiene que cambiar para que el refactor pase, el refactor ha cambiado el contrato.
"""
import time
from unittest.mock import AsyncMock

import httpx
import pytest

import main
from steam.rankings_repo import movers_repo, trending_repo
from steam.services import catalog_service
from stores import (
    _fx_cache, _item_history_cache, _item_price_cache,
    _market_index_cache, _market_lookup_cache, _market_prices_cache,
    _market_providers_cache, _search_cache, _topmovers_raw_cache,
)
from tests.test_steam_contract_rows import ITEM_KEYS as ROW_ITEM_KEYS, SAMPLE
from steam.mappers.row_mapper import _to_row

NAME = "AK-47 | Redline (Field-Tested)"
RAW = {
    "id": "1", "markethashname": NAME, "marketname": NAME, "pricelatestsell": 20.0,
    "pricereal": 19.0, "pricereal24h": 18.0, "sold24h": 50, "itemtype": "ak-47",
    "image": "https://cdn/ak.png", "prices": [{"market": "steam", "price": 20.0}],
}

# Salida de _map_item + enriquecimiento (precios por mercado, imagen, liquidez).
SKIN_CARD_KEYS = {
    "borderColor", "buffPrice", "buyOrderPrice", "buyOrderVolume", "csfloatPrice",
    "exterior", "externalPrices", "floatMax", "floatMin", "floatValue", "hoursToSold",
    "id", "image", "isSouvenir", "isStar", "isStatTrak", "itemName", "itemType",
    "liquidityBreakdown", "liquidityScore", "marketable", "name", "offerVolume",
    "paintIndex", "phase", "priceDelta24h", "priceDelta30d", "priceDelta7d",
    "priceLatest", "priceMax", "priceMin", "priceReal", "priceSafe", "quality", "rarity",
    "rarityColor", "slug", "sold24h", "sold30d", "sold7d", "soldTotal", "steamUrl",
    "tradable", "tradeLockDays", "weaponType",
}

CACHES = (
    _fx_cache, _item_history_cache, _item_price_cache,
    _market_index_cache, _market_lookup_cache, _market_prices_cache,
    _market_providers_cache, _search_cache, _topmovers_raw_cache,
)


def _handler(request: httpx.Request) -> httpx.Response:
    path, host = request.url.path, request.url.host
    if "frankfurter" in host:
        return httpx.Response(200, json={"rates": {"EUR": 0.9}})
    if path.endswith("/items"):
        return httpx.Response(200, json=[RAW])
    if path.endswith("/history"):
        return httpx.Response(200, json=[])
    if path.endswith("/info/markets"):
        return httpx.Response(200, json=[{"id": "csfloat", "name": "CSFloat", "logo": "https://l"}])
    if path.endswith("/prices"):
        return httpx.Response(200, json=[{"market_hash_name": NAME, "price": 19.5}])
    if path.endswith("/market-index/cs2"):
        return httpx.Response(200, json={
            "priceindex": [], "history": [], "turnover24h": 1.0, "sold24h": 2,
            "topmovers": {"gainers": [{"markethashname": NAME, "change24h": 5, "price": 20}], "losers": []},
        })
    return httpx.Response(404)


@pytest.fixture
def api(client, monkeypatch):
    for c in CACHES:
        c.clear()
    monkeypatch.setattr(main.app.state, "http_client", httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
    # El catálogo de ByMykel no forma parte del contrato de estos endpoints.
    monkeypatch.setattr(catalog_service, "fetch_static_images", AsyncMock())
    yield client
    for c in CACHES:
        c.clear()


def test_items(api):
    resp = api.get("/market/items?q=redline")
    assert resp.status_code == 200
    assert set(resp.json()[0]) == SKIN_CARD_KEYS


def test_items_sin_q_es_400(api):
    assert api.get("/market/items?q=").status_code == 400


def test_price(api):
    resp = api.get("/market/price", params={"name": NAME})
    assert resp.status_code == 200
    assert set(resp.json()) == SKIN_CARD_KEYS


def test_price_sin_match_exacto_es_404(api):
    assert api.get("/market/price", params={"name": "No existe"}).status_code == 404


def test_prices(api):
    resp = api.get("/market/prices?market=buff&name=AK")
    assert resp.status_code == 200
    assert set(resp.json()[0]) == {"market_hash_name", "price"}   # passthrough de steamwebapi


def test_fx(api):
    resp = api.get("/market/fx")
    assert resp.status_code == 200
    assert resp.json() == {"base": "USD", "rates": {"EUR": 0.9}, "stale": False}


def test_providers(api):
    resp = api.get("/market/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["id"] for p in body] == ["steam", "csfloat", "buff"]
    assert all(set(p) == {"id", "name", "logoUrl"} for p in body)


def test_index(api):
    resp = api.get("/market/index?tf=24h")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"history", "hottestItem", "sold24h", "turnover24h"}
    assert set(body["hottestItem"]) == {"name", "change24h", "price", "rarity", "rarityColor"}
    assert body["hottestItem"]["name"] == NAME


def test_movers_y_trending_sirven_snapshots(api, monkeypatch):
    rows = [_to_row(SAMPLE, 0, "hot"), _to_row(SAMPLE, 0, "cold")]
    monkeypatch.setattr(movers_repo, "fetch_snapshot", AsyncMock(return_value=rows))
    monkeypatch.setattr(trending_repo, "fetch_ranked", AsyncMock(return_value=rows[:1]))

    movers = api.get("/market/movers").json()
    assert set(movers) == {"hot", "cold"}
    assert set(movers["hot"][0]) == ROW_ITEM_KEYS

    trending = api.get("/market/trending").json()
    assert set(trending[0]) == ROW_ITEM_KEYS


# ── /market/index: formatos y errores (fixture `steam_api` de conftest) ─────────

INDEX_POINT_KEYS = {"date", "price", "change", "volume"}
POINT = {"createdat": "2026-10-01T00:00:00", "priceindex": 1.2, "change": 0.1, "sold": 5}


@pytest.mark.parametrize("body", [
    [POINT],                                                     # lista suelta
    {"history": [POINT], "topmovers": {}},                       # history lista
    {"history": {"priceindex": [POINT]}, "turnover24h": "3.5"},  # history dict
])
def test_index_formatos(steam_api, client, body):
    steam_api.on("market-index/cs2", json=body)
    resp = client.get("/market/index?tf=7d")
    assert resp.status_code == 200
    out = resp.json()
    assert set(out) == {"history", "hottestItem", "sold24h", "turnover24h"}
    assert all(set(p) == INDEX_POINT_KEYS for p in out["history"])
    assert out["hottestItem"] == {"name": "—", "change24h": 0.0, "price": None,
                                  "rarity": None, "rarityColor": None}


@pytest.mark.parametrize("route, status", [
    ({"exc": httpx.ReadTimeout("t")}, 504),
    ({"exc": httpx.ConnectError("down")}, 502),
    ({"status": 500}, 502),
    ({"json": "texto"}, 502),
    ({"json": {"history": "texto"}}, 502),
    ({"json": {"history": {"priceindex": "texto"}}}, 502),
])
def test_index_errores(steam_api, client, route, status):
    steam_api.on("market-index/cs2", **route)
    assert client.get("/market/index").status_code == status


def test_index_402_sirve_cache_caducada_o_503(steam_api, client, monkeypatch):
    steam_api.on("market-index/cs2", status=402)
    resp = client.get("/market/index")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "upstream_quota"

    steam_api.on("market-index/cs2", json=[POINT])
    real = time.monotonic
    with monkeypatch.context() as m:
        m.setattr(time, "monotonic", lambda: real() - 2 * 86400)
        bueno = client.get("/market/index").json()   # cacheado hace dos días
    steam_api.on("market-index/cs2", status=402)
    resp = client.get("/market/index")
    assert resp.status_code == 200
    assert resp.json() == bueno


def test_index_gainer_sin_nombre_no_es_500(steam_api, client):
    steam_api.on("market-index/cs2", json={"history": [], "topmovers": {"gainers": [{"price": 1}]}})
    resp = client.get("/market/index")
    assert resp.status_code == 200
    assert resp.json()["hottestItem"]["name"] == "—"
