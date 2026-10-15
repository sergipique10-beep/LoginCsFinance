"""UX-46: toda ruta de steam/ que sirve un `Fetched` degradado lo dice en `X-Data-Status`
y `X-Data-Reason` (más `X-Data-Captured-At` si se sabe la fecha del dato). El cuerpo no
cambia: eso lo fijan los tests de contrato de CAL-09. Con dato bueno no hay cabecera.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from settings import ALLOWED_CORS_ORIGINS
from steam.cache.history_cache import _item_history_cache
from steam.cache.market_cache import (
    _item_price_cache, _market_index_cache, _market_prices_cache, _search_cache,
)
from steam.rankings_repo import movers_repo, trending_repo
from steam.routes._response import DATA_HEADERS, data_headers, respond
from steam.domain.models import Fetched
from tests.conftest import SNAPSHOT_DB, STEAM_ID
from tests.test_steam_contract_market import NAME, RAW


def _status(resp) -> tuple[str | None, str | None]:
    return resp.headers.get("X-Data-Status"), resp.headers.get("X-Data-Reason")


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


# ── Helper ────────────────────────────────────────────────────────────────────

def test_ok_no_lleva_cabeceras_y_devuelve_el_cuerpo_tal_cual():
    assert data_headers("ok", "x", "2026-01-01") == {}
    assert respond(Fetched([1])) == [1]


def test_degradado_sin_reason_dice_unknown_y_sin_fecha_no_la_inventa():
    assert data_headers("stale", None) == {"X-Data-Status": "stale", "X-Data-Reason": "unknown"}
    resp = respond(Fetched({"a": 1}, "partial", "backoff"), captured_at="2026-01-01")
    assert resp.headers["X-Data-Captured-At"] == "2026-01-01"


def test_cors_expone_las_cabeceras(steam_api, client):
    # Sin esto el WebView de Capacitor no las lee (el tropiezo de PERF-14).
    resp = client.get("/market/providers", headers={"Origin": ALLOWED_CORS_ORIGINS[0]})
    exposed = resp.headers.get("access-control-expose-headers", "")
    assert all(h in exposed for h in DATA_HEADERS)


# ── Rutas con dato bueno ─────────────────────────────────────────────────────

def test_busqueda_ok_sin_cabeceras(steam_api, client):
    steam_api.on("api/items", json=[RAW])
    resp = client.get("/market/items?q=redline")
    assert resp.status_code == 200
    assert _status(resp) == (None, None)


# ── Stale por 402 (SEC-16) ───────────────────────────────────────────────────

def test_busqueda_y_precio_stale(steam_api, client):
    _search_cache.put("market:redline", [{"name": NAME}], now=-1e9)
    _item_price_cache.put(NAME.lower(), {"name": NAME}, now=-1e9)
    steam_api.on("api/items", status=402)
    search = client.get("/market/items?q=redline")
    price = client.get("/market/price", params={"name": NAME})
    assert search.json() == [{"name": NAME}] and price.json() == {"name": NAME}
    assert _status(search) == _status(price) == ("stale", "quota")


def test_indice_y_precios_por_mercado_stale(steam_api, client):
    _market_index_cache.put("24h", {"x": 1}, now=-1e9)
    _market_prices_cache.put("buff::usd", [{"p": 1}], now=-1e9)
    steam_api.on("market-index/cs2", status=402)
    steam_api.on("buff/prices", status=402)
    assert _status(client.get("/market/index")) == ("stale", "quota")
    assert _status(client.get("/market/prices?market=buff")) == ("stale", "quota")


# ── Respaldo, vacío y error ──────────────────────────────────────────────────

def test_proveedores_lista_estatica_es_partial(steam_api, client):
    steam_api.on("info/markets", status=500)
    resp = client.get("/market/providers")
    assert [p["id"] for p in resp.json()] == ["steam", "csfloat", "buff"]
    assert _status(resp) == ("partial", "http_500")


def test_fx_sin_tasa_conserva_stale_en_el_cuerpo_y_lleva_cabecera(steam_api, client):
    steam_api.on("v1/latest", status=500)
    resp = client.get("/market/fx")
    assert resp.json() == {"base": "USD", "rates": {}, "stale": True}   # UX-08, sin cambios
    assert _status(resp) == ("error", "http_500")


def test_fx_ok_sin_cabeceras(steam_api, client):
    steam_api.on("v1/latest", json={"rates": {"EUR": 0.9}})
    resp = client.get("/market/fx")
    assert resp.json()["stale"] is False
    assert _status(resp) == (None, None)


def test_item_history_stale_y_formato_inesperado(steam_api, client):
    _item_history_cache.put(f"{NAME}:10:steam:35", [{"date": "d", "price": 1.0, "volume": 1}], now=-1e9)
    steam_api.on("api/history", status=429)
    assert _status(client.get("/item/history", params={"name": NAME})) == ("stale", "rate_limit")
    steam_api.on("api/history", json={"no": "lista"})
    resp = client.get("/item/history", params={"name": "otra"})
    assert resp.json() == []
    assert _status(resp) == ("error", "unexpected_format")


def test_perfil_vacio(steam_api, client):
    steam_api.on("api/profile", json=[])
    assert _status(client.get("/me")) == ("error", "empty_body")


def test_noticias_sin_legibles_es_partial(steam_api, client):
    steam_api.on("GetNewsForApp/v2/", json={"appnews": {"newsitems": [
        {"gid": "1", "title": "Обновление", "url": "", "contents": "", "date": 0},
    ]}})
    resp = client.get("/news/cs2")
    assert [n["title"] for n in resp.json()] == ["Обновление"]
    assert _status(resp) == ("partial", "no_readable_news")


# ── Inventario: las genéricas conviven con X-Inventory-* (PERF-14) ────────────

def test_inventario_snapshot_lleva_las_dos_familias(steam_api, client):
    SNAPSHOT_DB[STEAM_ID] = ([{"name": NAME}], "2026-10-03T10:00:00+00:00")
    steam_api.on("api/inventory", status=402)
    resp = client.get("/inventory")
    assert resp.headers["X-Inventory-Stale"] == "1"
    assert _status(resp) == ("stale", "quota")
    assert resp.headers["X-Data-Captured-At"] == resp.headers["X-Inventory-Captured-At"]


def test_inventario_410_sin_snapshot_es_error_con_lista_vacia(steam_api, client):
    steam_api.on("api/inventory", status=410)
    resp = client.get("/inventory")
    assert resp.json() == []
    assert _status(resp) == ("error", "http_410")
    assert "X-Inventory-Stale" not in resp.headers


def test_inventario_ok_sin_cabeceras(steam_api, client):
    steam_api.on("api/inventory", json=[RAW])
    resp = client.get("/inventory")
    assert resp.status_code == 200
    assert _status(resp) == (None, None)


# ── Movers y trending: edad del snapshot (opción A) ───────────────────────────

def _movers(updated_at: str | None) -> list[dict]:
    row = {"name": NAME, "bucket": "hot", "rank": 0, "price_latest": 1.0}
    return [row | ({"updated_at": updated_at} if updated_at else {})]


@pytest.mark.parametrize("age, expected", [
    ({"minutes": 10}, (None, None)),
    ({"minutes": 50}, ("stale", "snapshot_age")),
])
def test_movers_por_edad(client, monkeypatch, age, expected):
    ts = _ago(**age)
    monkeypatch.setattr(movers_repo, "fetch_snapshot", AsyncMock(return_value=_movers(ts)))
    resp = client.get("/market/movers")
    assert [i["name"] for i in resp.json()["hot"]] == [NAME]
    assert _status(resp) == expected
    if expected[0]:
        assert resp.headers["X-Data-Captured-At"] == ts


def test_movers_vacio_es_error(client, monkeypatch):
    monkeypatch.setattr(movers_repo, "fetch_snapshot", AsyncMock(return_value=[]))
    resp = client.get("/market/movers")
    assert resp.json() == {"hot": [], "cold": []}
    assert _status(resp) == ("error", "empty_snapshot")


def test_movers_sin_fecha_no_se_marca(client, monkeypatch):
    monkeypatch.setattr(movers_repo, "fetch_snapshot", AsyncMock(return_value=_movers(None)))
    assert _status(client.get("/market/movers")) == (None, None)


@pytest.mark.parametrize("age, expected", [({"hours": 1}, None), ({"hours": 4}, "stale")])
def test_trending_por_edad_de_la_fila_mas_reciente(client, monkeypatch, age, expected):
    rows = [{"name": "viejo", "seen_at": _ago(days=6)}, {"name": NAME, "seen_at": _ago(**age)}]
    monkeypatch.setattr(trending_repo, "fetch_ranked", AsyncMock(return_value=rows))
    resp = client.get("/market/trending")
    assert len(resp.json()) == 2
    assert resp.headers.get("X-Data-Status") == expected
