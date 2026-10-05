"""CAL-09 (Fase 0): contrato de las tools del chat que tiran de steam/
(`tools/market_tools.py`, `tools/inventory_tools.py`), con las APIs externas simuladas
por HTTP (fixture `steam_api`).

Lo que se congela es lo que recibe el modelo: claves, filtros y caché. Durante
STEAM-REFACTOR estos tests no se tocan.
"""
from unittest.mock import AsyncMock

import pytest

from steam.mappers.row_mapper import _to_row
from steam.rankings_repo import movers_repo, trending_repo
from tests.test_steam_contract_market import NAME, RAW, SKIN_CARD_KEYS
from tests.test_steam_contract_rows import SAMPLE
from tools import registry
from tools.inventory_tools import _ver_inventario, register_inventory_tools
from tools.market_tools import (
    _buscar_skin, _consultar_precio_skin, _historial_precio, _ver_movers, _ver_trending,
    register_market_tools,
)

CSFLOAT = [{"createdat": "2026-10-01T00:00:00", "price": 20.0, "quantity": 2}]
RESUMEN_KEYS = {"total_items", "distintos", "valor_total", "sin_precio", "items", "omitidos"}
FILA_RESUMEN_KEYS = {"name", "cantidad", "priceLatest", "priceDelta24h", "priceDelta7d", "liquidityScore"}
BARATO = {**RAW, "markethashname": "Gratis", "marketname": "Gratis", "pricelatestsell": 0}
SLAB = {**RAW, "markethashname": "Sticker Slab | X", "marketname": "Sticker Slab | X"}


@pytest.fixture
def api(steam_api):
    """El http_client simulado, para llamar a las tools directamente."""
    import main
    return main.app.state.http_client


# ── consultar_precio_skin ─────────────────────────────────────────────────────

async def test_consultar_precio_skin(steam_api, api):
    steam_api.on("api/items", json=[RAW])
    steam_api.on("csfloat/history", json=CSFLOAT)
    item = await _consultar_precio_skin(market_hash_name=f"  {NAME.upper()} ", client=api)
    assert set(item) == SKIN_CARD_KEYS
    assert await _consultar_precio_skin(market_hash_name=NAME, client=api) == item
    assert len(steam_api.hits("api/items")) == 1   # la segunda sale de _item_price_cache


@pytest.mark.parametrize("body, error", [
    ([RAW], "skin 'Otra' no encontrada"),
    ({"x": 1}, "formato inesperado de Steam API"),
])
async def test_consultar_precio_skin_sin_resultado(steam_api, api, body, error):
    steam_api.on("api/items", json=body)
    assert await _consultar_precio_skin(market_hash_name="Otra", client=api) == {"error": error}


async def test_consultar_precio_skin_402_explica_la_cuota(steam_api, api):
    steam_api.on("api/items", status=402)
    assert "error" in await _consultar_precio_skin(market_hash_name=NAME, client=api)


# ── buscar_skin ───────────────────────────────────────────────────────────────

async def test_buscar_skin(steam_api, api):
    steam_api.on("api/items", json=[BARATO, RAW, SLAB])
    out = await _buscar_skin(query=" redline ", client=api)
    assert [r["name"] for r in out] == [NAME]   # fuera precio 0 y slabs
    # Sin liquidityScore: el select corto del chat no trae sus campos y sale a None (CAL-11).
    assert set(out[0]) == {"name", "priceLatest", "priceDelta24h", "sold24h", "rarity", "itemType"}
    assert await _buscar_skin(query="REDLINE", client=api) == out
    assert len(steam_api.hits("api/items")) == 1   # la segunda sale de _search_cache


@pytest.mark.parametrize("query, body", [("   ", [RAW]), ("redline", {"x": 1})])
async def test_buscar_skin_vacio(steam_api, api, query, body):
    steam_api.on("api/items", json=body)
    assert await _buscar_skin(query=query, client=api) == []


async def test_buscar_skin_no_contamina_la_busqueda_de_market(steam_api, api, client):
    steam_api.on("api/items", json=[RAW])
    await _buscar_skin(query="redline", client=api)
    client.get("/market/items?q=redline")
    assert len(steam_api.hits("api/items")) == 2


# ── ver_trending, ver_movers, historial_precio ────────────────────────────────

async def test_ver_trending_y_movers(api, monkeypatch):
    rows = [_to_row(SAMPLE, 0, "hot"), _to_row(SAMPLE, 0, "cold")]
    monkeypatch.setattr(trending_repo, "fetch_snapshot", AsyncMock(return_value=rows[:1]))
    monkeypatch.setattr(movers_repo, "fetch_snapshot", AsyncMock(return_value=rows))
    campos = {"name", "priceLatest", "priceDelta24h", "priceDelta7d", "priceDelta30d", "sold24h",
              "rarity", "itemType"}

    (trending,) = await _ver_trending(client=api)
    assert set(trending) == campos
    movers = await _ver_movers(client=api)
    assert set(movers) == {"hot", "cold"}
    assert [set(movers["hot"][0]), set(movers["cold"][0])] == [campos, campos]


async def test_historial_precio(steam_api, api):
    steam_api.on("csfloat/history", json=CSFLOAT)
    assert await _historial_precio(market_hash_name=NAME, client=api) == [
        {"date": "2026-10-01", "price": 20.0, "volume": 2},
    ]


def test_registro_de_tools(monkeypatch):
    monkeypatch.setattr(registry, "_tool_registry", {})
    register_market_tools()
    register_inventory_tools()
    assert sorted(registry.list_tools()) == [
        "buscar_skin", "consultar_precio_skin", "historial_precio", "ver_inventario",
        "ver_movers", "ver_trending",
    ]


# ── ver_inventario ────────────────────────────────────────────────────────────

async def test_ver_inventario(steam_api, api):
    steam_api.on("api/inventory", json=[RAW, RAW, BARATO])
    out = await _ver_inventario(steam_id="1", client=api)
    assert set(out) == RESUMEN_KEYS
    assert (out["total_items"], out["distintos"], out["sin_precio"]) == (3, 2, 1)
    assert set(out["items"][0]) == FILA_RESUMEN_KEYS

    filtrado = await _ver_inventario(steam_id="1", client=api, buscar="gratis")
    assert [f["name"] for f in filtrado["items"]] == ["Gratis"]
    assert len(steam_api.hits("api/inventory")) == 1   # la segunda sale de _inventory_cache


@pytest.mark.parametrize("route", [{"status": 403}, {"status": 429}, {"exc": RuntimeError("red")}])
async def test_ver_inventario_explica_el_error(steam_api, api, route):
    steam_api.on("api/inventory", **route)
    assert "error" in await _ver_inventario(steam_id="1", client=api)
