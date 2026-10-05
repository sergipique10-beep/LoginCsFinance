"""CLEAN-12: cada degradación que el cliente no ve deja UNA línea
`[steam-degraded] flow= reason= served= last_hour=` en los logs (una por fila "invisible"
del mapa de degradaciones de docs/features/steam.md). La respuesta no cambia: eso lo
fijan los tests de contrato de CAL-09.
"""
import logging
import re
import time

import httpx
import pytest

from steam.errors import handling as degraded
from steam.services import catalog, market as market_service, pricing
from steam.services import providers as providers_service
from stores import _item_history_cache, _item_price_cache, _market_lookup_cache, _search_cache
from tests.test_steam_contract_market import NAME, RAW
from tools.inventory_tools import _ver_inventario

LINE = re.compile(r"\[steam-degraded\] flow=(\S+) reason=(\S+) served=(\S+) last_hour=(\d+)")
TOPMOVERS = {"topmovers": {"gainers": [{"markethashname": "G", "price": 1, "change24h": 5}], "losers": []}}


@pytest.fixture
def lines(caplog):
    caplog.set_level(logging.WARNING, logger="uvicorn.error")
    degraded._recent.clear()

    def _get(flow: str) -> list[tuple[str, str]]:
        found = [LINE.search(r.getMessage()) for r in caplog.records]
        return [(m.group(2), m.group(3)) for m in found if m and m.group(1) == flow]
    return _get


@pytest.fixture
def http(steam_api):
    import main
    return main.app.state.http_client


def test_formato_y_conteo_de_la_ultima_hora(lines, caplog):
    degraded.log_degraded("x", "r", "stale")
    degraded.log_degraded("x", "r", "stale")
    degraded.log_degraded("x", "otro", "empty")
    counts = [LINE.search(r.getMessage()).group(4) for r in caplog.records]
    assert counts == ["1", "2", "1"]
    degraded._recent[("x", "r")][0] = time.time() - 3601     # una de hace más de una hora
    degraded.log_degraded("x", "r", "stale")
    assert LINE.search(caplog.records[-1].getMessage()).group(4) == "2"


# ── Inventario y perfil ───────────────────────────────────────────────────────

def test_inventario_410(steam_api, client, lines):
    steam_api.on("api/inventory", status=410)
    assert client.get("/inventory").json() == []
    assert lines("inventory") == [("http_410", "empty")]


def test_perfil_vacio(steam_api, client, lines):
    steam_api.on("api/profile", json=[])
    client.get("/me")
    assert lines("profile") == [("empty_body", "empty")]


@pytest.mark.parametrize("route", [
    {"status": 403}, {"exc": httpx.ConnectError("x")}, {"json": {"no": "lista"}},
])
async def test_inventario_del_chat_no_deja_linea(steam_api, http, lines, route):
    # CAL-14 (CLEAN-15): el fallo vuelve al modelo como `{"error": ...}`, así que el
    # cliente lo ve y no hay degradación invisible que registrar.
    steam_api.on("api/inventory", **route)
    assert "error" in await _ver_inventario(steam_id="1", client=http)
    assert lines("chat_inventory") == []


# ── Histórico ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("route, reason", [({"status": 500}, "http_500"), ({"json": {}}, "unexpected_format"),
                                           ({"exc": httpx.ReadTimeout("t")}, "timeout")])
async def test_historico_del_enriquecimiento(steam_api, http, lines, route, reason):
    steam_api.on("csfloat/history", **route)
    assert await pricing.fetch_history_for_item(http, NAME) == []
    assert lines("history") == [(reason, "empty")]


def test_item_history_402_stale_y_cuerpo_que_no_es_lista(steam_api, client, lines):
    # CAL-14 (CLEAN-15): el 402 sin caché ya es un 503 visible (sin línea); con caché
    # caducada se sirve stale y se registra, igual que el 429.
    _item_history_cache.put(f"{NAME}:10:steam:35", [{"date": "d", "price": 1.0, "volume": 1}], now=-1e9)
    steam_api.on("api/history", status=402)
    assert client.get("/item/history", params={"name": NAME}).status_code == 200
    assert client.get("/item/history", params={"name": "sin-cache"}).status_code == 503
    steam_api.on("api/history", json={"no": "lista"})
    assert client.get("/item/history", params={"name": "otra"}).json() == []
    assert lines("item_history") == [("quota", "stale"), ("unexpected_format", "empty")]


def test_item_history_stale_por_429(steam_api, client, lines):
    _item_history_cache.put(f"{NAME}:10:steam:35", [{"date": "d", "price": 1.0, "volume": 1}], now=-1e9)
    steam_api.on("api/history", status=429)
    assert client.get("/item/history", params={"name": NAME}).status_code == 200
    assert lines("item_history") == [("rate_limit", "stale")]


# ── Lookup, proveedores y catálogo ────────────────────────────────────────────

async def test_lookup_de_mercado_fallo_y_backoff(steam_api, http, lines):
    steam_api.on("csfloat/prices", status=500)
    assert await pricing._fetch_market_price_lookup(http, "csfloat") == {}
    assert await pricing._fetch_market_price_lookup(http, "csfloat") == {}    # en backoff
    _market_lookup_cache.put("buff", {"AK": 1.0}, now=-1e9)
    steam_api.on("buff/prices", exc=httpx.ConnectError("x"))
    assert await pricing._fetch_market_price_lookup(http, "buff") == {"AK": 1.0}
    assert lines("market_lookup") == [("http_500", "empty"), ("backoff", "empty"), ("unavailable", "stale")]


async def test_proveedores_respaldo(steam_api, http, lines):
    steam_api.on("info/markets", status=500)
    assert [p["id"] for p in (await providers_service.fetch_market_providers(http)).data] == ["steam", "csfloat", "buff"]
    assert lines("providers") == [("http_500", "fallback")]


async def test_catalogo_todas_las_fuentes_caidas(steam_api, http, lines, monkeypatch):
    import asyncio
    monkeypatch.setattr(catalog, "_image_cache_lock", asyncio.Lock())
    steam_api.on(".json", status=500)
    await catalog.fetch_static_images(http)
    assert lines("catalog") == [("all_sources_failed", "empty")]


# ── Rankings ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("compute, flow", [
    (market_service.compute_movers, "movers"), (market_service.compute_trending, "trending"),
])
async def test_rankings_caen_a_topmovers(steam_api, http, lines, compute, flow):
    steam_api.on("api/items", status=500)
    steam_api.on("market-index/cs2", json=TOPMOVERS)
    await compute(http)
    assert lines(flow) == [("http_500", "fallback")]


@pytest.mark.parametrize("compute, flow", [
    (market_service.compute_movers, "movers"), (market_service.compute_trending, "trending"),
])
async def test_rankings_sin_ninguna_fuente(steam_api, http, lines, compute, flow):
    steam_api.on("api/items", exc=httpx.ReadTimeout("t"))
    steam_api.on("market-index/cs2", status=500)
    await compute(http)
    assert lines(flow) == [("timeout", "error")]


# ── Stale ante 402 (el cliente recibe un 200 normal) ──────────────────────────

def test_busqueda_y_precio_stale_por_402(steam_api, client, lines):
    _search_cache.put("redline", [{"name": NAME}], now=-1e9)
    _item_price_cache.put(NAME.lower(), {"name": NAME}, now=-1e9)
    steam_api.on("api/items", status=402)
    assert client.get("/market/items?q=redline").json() == [{"name": NAME}]
    assert client.get("/market/price", params={"name": NAME}).json() == {"name": NAME}
    assert lines("search") == [("quota", "stale")]
    assert lines("item_price") == [("quota", "stale")]


def test_indice_y_precios_por_mercado_stale_por_402(steam_api, client, lines, monkeypatch):
    from stores import _market_index_cache, _market_prices_cache
    _market_index_cache.put("24h", {"x": 1}, now=-1e9)
    _market_prices_cache.put("buff::usd", [{"p": 1}], now=-1e9)
    steam_api.on("market-index/cs2", status=402)
    steam_api.on("buff/prices", status=402)
    assert client.get("/market/index").json() == {"x": 1}
    assert client.get("/market/prices?market=buff").json() == [{"p": 1}]
    assert lines("market_index") == [("quota", "stale")]
    assert lines("market_prices") == [("quota", "stale")]


# ── Noticias ──────────────────────────────────────────────────────────────────

def test_noticia_sin_og_image(steam_api, client, lines):
    steam_api.on("GetNewsForApp/v2/", json={"appnews": {"newsitems": [
        {"gid": "1", "title": "A", "url": "https://news/1", "contents": "", "date": 0},
        {"gid": "2", "title": "B", "url": "", "contents": "", "date": 0},
    ]}})
    client.get("/news/cs2")
    assert lines("news_image") == [("http_404", "empty")]   # la que no tiene url no cuenta


def test_sin_degradacion_no_hay_linea(steam_api, client, lines):
    steam_api.on("api/items", json=[RAW])
    steam_api.on("csfloat/prices", json=[])
    steam_api.on("buff/prices", json=[])
    steam_api.on("csfloat/history", json=[])
    client.get("/market/items?q=redline")
    assert lines("search") == [] and lines("market_lookup") == []
