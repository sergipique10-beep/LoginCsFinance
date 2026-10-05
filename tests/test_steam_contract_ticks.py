"""CAL-09 (Fase 0): contrato de los ticks de mercado (cap, trending, enrich, movers) y de
los rankings que calculan, con steamwebapi simulado por HTTP (fixture `steam_api`) y
los repos de Supabase sustituidos.

Se congela la respuesta de cada tick y QUÉ items acaban en cada tabla y en qué orden
(filtros de precio, ventas y slab, orden por turnover, hot/cold por delta 7d, fallback
de topmovers). Durante STEAM-REFACTOR estos tests no se tocan.
"""
import time
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from steam import cap_history_repo, price_history_repo
from steam.rankings_repo import movers_repo, trending_repo
from steam.routes import market as market_routes

TOKEN = {"X-Cap-Token": "secret123"}
TIMEOUT = httpx.ReadTimeout("timeout")
TODAY = date.today()


def _raw(name, price, sold, itemtype, marketname=None):
    return {"markethashname": name, "marketname": marketname or name, "pricelatestsell": price,
            "pricereal": price, "sold24h": sold, "itemtype": itemtype}


AK = _raw("AK-47 | Redline (Field-Tested)", 30.0, 100, "rifle")
AWP = _raw("AWP | Asiimov (Field-Tested)", 50.0, 10, "sniper rifle")
GLOCK = _raw("Glock-18 | Fade (Factory New)", 15.0, 20, "pistol")
KNIFE = _raw("★ Karambit | Doppler (Factory New)", 300.0, 5, "knife")
BARATO = _raw("P250 | Sand Dune (Field-Tested)", 5.0, 1000, "pistol")
POCAS_VENTAS = _raw("M4A4 | Howl (Field-Tested)", 100.0, 4, "rifle")
SLAB = _raw("Sticker Slab | Crown (Foil)", 20.0, 50, "sticker")

# Delta 7d que devuelve el histórico de csfloat para cada skin (None = sin histórico).
DELTA_7D = {AK["markethashname"]: 10.0, AWP["markethashname"]: -5.0, GLOCK["markethashname"]: 2.0}


def _history(request: httpx.Request) -> httpx.Response:
    delta = DELTA_7D.get(request.url.params["market_hash_name"])
    if delta is None:
        return httpx.Response(200, json=[])
    return httpx.Response(200, json=[
        {"createdat": (TODAY - timedelta(days=8)).isoformat(), "price": 100.0, "quantity": 1},
        {"createdat": TODAY.isoformat(), "price": 100.0 + delta, "quantity": 1},
    ])


TOPMOVERS = {"topmovers": {
    "gainers": [
        {"markethashname": "Gainer B", "price": 2.0, "change24h": 20},
        {"markethashname": "Sticker Slab | X", "price": 1.0, "change24h": 99},
        {"markethashname": "Gainer A", "price": 1.0, "change24h": 50},
    ],
    "losers": [
        {"markethashname": "Loser A", "price": 3.0, "change24h": -10},
        {"markethashname": "Loser B", "price": 4.0, "change24h": -40},
    ],
}}


@pytest.fixture
def repos(steam_api, monkeypatch):
    monkeypatch.setattr(market_routes, "CAP_TICK_TOKEN", "secret123")
    fakes = {
        "replace_snapshot": AsyncMock(),
        "upsert_rows": AsyncMock(),
        "purge_stale": AsyncMock(return_value=3),
        "fetch_stalest": AsyncMock(return_value=[]),
        "register_tracked": AsyncMock(),
    }
    monkeypatch.setattr(movers_repo, "replace_snapshot", fakes["replace_snapshot"])
    monkeypatch.setattr(trending_repo, "upsert_rows", fakes["upsert_rows"])
    monkeypatch.setattr(trending_repo, "purge_stale", fakes["purge_stale"])
    monkeypatch.setattr(trending_repo, "fetch_stalest", fakes["fetch_stalest"])
    monkeypatch.setattr(price_history_repo, "register_tracked", fakes["register_tracked"])
    steam_api.on("csfloat/history", fn=_history)
    return fakes


def _rows(mock):
    return mock.await_args.args[0]


@pytest.mark.parametrize("path", [
    "/internal/cap-tick", "/internal/trending-tick", "/internal/enrich-tick", "/internal/movers-tick",
])
def test_ticks_sin_token_son_401(repos, client, path):
    assert client.post(path).status_code == 401
    assert client.post(path, headers={"X-Cap-Token": "otro"}).status_code == 401


# ── cap-tick ──────────────────────────────────────────────────────────────────

def test_cap_tick(steam_api, repos, client, monkeypatch):
    supabase = MagicMock()
    monkeypatch.setattr(cap_history_repo, "get_supabase", lambda: supabase)
    steam_api.on("market-index/cs2", json={
        "priceindex": 1.5, "realpriceindex": 1.4, "buyorderpriceindex": None, "turnover24h": 10,
    })
    resp = client.post("/internal/cap-tick", headers=TOKEN)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ok", "ts", "priceindex"}
    assert body["ok"] is True and body["priceindex"] == 1.5
    assert body["ts"].endswith(":00:00Z")   # floor a la hora: upsert idempotente

    point = supabase.table.return_value.upsert.call_args.args[0]
    assert point == {"ts": body["ts"], "priceindex": 1.5, "realpriceindex": 1.4,
                     "buyorderpriceindex": None, "turnover24h": 10.0}
    assert supabase.table.return_value.upsert.call_args.kwargs == {"on_conflict": "ts"}


@pytest.mark.parametrize("route", [
    {"exc": TIMEOUT}, {"exc": httpx.ConnectError("down")}, {"status": 500}, {"json": []}, {"json": {}},
])
def test_cap_tick_errores_son_502(steam_api, repos, client, route):
    steam_api.on("market-index/cs2", **route)
    assert client.post("/internal/cap-tick", headers=TOKEN).status_code == 502


# ── movers-tick ───────────────────────────────────────────────────────────────

def test_movers_tick_desde_items(steam_api, repos, client):
    steam_api.on("api/items", json=[BARATO, AK, POCAS_VENTAS, AWP, SLAB, GLOCK, KNIFE])
    resp = client.post("/internal/movers-tick", headers=TOKEN)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "count": 8}

    rows = _rows(repos["replace_snapshot"])
    por_bucket = {b: [(r["name"], r["rank"]) for r in rows if r["bucket"] == b] for b in ("hot", "cold")}
    ak, awp, glock, knife = (x["markethashname"] for x in (AK, AWP, GLOCK, KNIFE))
    # Fuera: < $10.80, < 5 ventas y slabs. Hot: delta 7d desc; cold: asc; sin delta al final.
    assert por_bucket["hot"] == [(ak, 0), (glock, 1), (awp, 2), (knife, 3)]
    assert por_bucket["cold"] == [(awp, 0), (glock, 1), (ak, 2), (knife, 3)]
    deltas = {r["name"]: r["price_delta_7d"] for r in rows if r["bucket"] == "hot"}
    assert deltas == {ak: 10.0, glock: 2.0, awp: -5.0, knife: None}


@pytest.mark.parametrize("items_route", [{"status": 500}, {"exc": TIMEOUT}, {"json": {"error": "x"}}])
def test_movers_tick_fallback_topmovers(steam_api, repos, client, items_route):
    steam_api.on("api/items", **items_route)
    steam_api.on("market-index/cs2", json=TOPMOVERS)
    resp = client.post("/internal/movers-tick", headers=TOKEN)
    assert resp.json() == {"ok": True, "count": 4}

    rows = _rows(repos["replace_snapshot"])
    assert [r["name"] for r in rows if r["bucket"] == "hot"] == ["Gainer A", "Gainer B"]
    assert [r["name"] for r in rows if r["bucket"] == "cold"] == ["Loser B", "Loser A"]
    assert {r["price_delta_7d"] for r in rows} == {0.0}   # UX-46: deltas fijos


def test_movers_tick_fallback_reutiliza_topmovers_de_market_index(steam_api, repos, client):
    steam_api.on("market-index/cs2", json={**TOPMOVERS, "history": []})
    assert client.get("/market/index").status_code == 200
    steam_api.on("api/items", status=500)
    steam_api.on("market-index/cs2", status=500)
    assert client.post("/internal/movers-tick", headers=TOKEN).json() == {"ok": True, "count": 4}


def test_movers_tick_no_usa_un_topmovers_de_hace_dias(steam_api, repos, client, monkeypatch):
    steam_api.on("market-index/cs2", json={**TOPMOVERS, "history": []})
    real = time.monotonic
    with monkeypatch.context() as m:
        m.setattr(time, "monotonic", lambda: real() - 3 * 86400)
        client.get("/market/index")   # cachea el topmovers "hace 3 días"
    steam_api.on("api/items", status=500)
    steam_api.on("market-index/cs2", status=500)
    assert client.post("/internal/movers-tick", headers=TOKEN).json()["ok"] is False


def test_movers_tick_items_con_json_invalido_no_es_500(steam_api, repos, client):
    steam_api.on("api/items", content=b"<html>")
    steam_api.on("market-index/cs2", json=TOPMOVERS)
    assert client.post("/internal/movers-tick", headers=TOKEN).status_code == 200


# ── trending-tick ─────────────────────────────────────────────────────────────

def test_trending_tick_desde_items(steam_api, repos, client):
    steam_api.on("api/items", json=[BARATO, GLOCK, POCAS_VENTAS, AWP, AK, KNIFE])
    resp = client.post("/internal/trending-tick", headers=TOKEN)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "count": 5, "purged": 3, "tracked": 5}

    rows = _rows(repos["upsert_rows"])
    # Fuera solo lo barato (en trending basta 1 venta). Orden: turnover = precio × ventas 24h.
    orden = [AK, KNIFE, AWP, POCAS_VENTAS, GLOCK]
    assert [(r["name"], r["rank"]) for r in rows] == [(x["markethashname"], i) for i, x in enumerate(orden)]
    assert all("seen_at" in r and "bucket" not in r for r in rows)
    # Sin deltas de csfloat en la captura: esos los pone enrich-tick.
    assert not repos["upsert_rows"].await_args.kwargs
    assert len(steam_api.hits("csfloat/history")) == 0
    repos["purge_stale"].assert_awaited_once_with(7)
    repos["register_tracked"].assert_awaited_once_with([x["markethashname"] for x in orden], "trending")


def test_trending_tick_fallback_topmovers(steam_api, repos, client):
    steam_api.on("api/items", status=500)
    steam_api.on("market-index/cs2", json=TOPMOVERS)
    resp = client.post("/internal/trending-tick", headers=TOKEN)
    assert resp.json()["count"] == 5
    assert {r["name"] for r in _rows(repos["upsert_rows"])} == {
        g["markethashname"] for g in TOPMOVERS["topmovers"]["gainers"] + TOPMOVERS["topmovers"]["losers"]
    }


def test_trending_tick_sin_fuentes_no_inserta_pero_purga(steam_api, repos, client):
    steam_api.on("api/items", status=500)
    steam_api.on("market-index/cs2", status=500)
    resp = client.post("/internal/trending-tick", headers=TOKEN)
    assert resp.json() == {"ok": True, "count": 0, "purged": 3, "tracked": 0}
    assert _rows(repos["upsert_rows"]) == []
    repos["register_tracked"].assert_not_awaited()


def test_trending_tick_items_con_json_invalido_no_es_500(steam_api, repos, client):
    steam_api.on("api/items", content=b"<html>")
    steam_api.on("market-index/cs2", json=TOPMOVERS)
    assert client.post("/internal/trending-tick", headers=TOKEN).status_code == 200


# ── enrich-tick ───────────────────────────────────────────────────────────────

def test_enrich_tick_sin_pendientes(repos, client):
    assert client.post("/internal/enrich-tick", headers=TOKEN).json() == {"ok": True, "count": 0, "with_deltas": 0}
    repos["upsert_rows"].assert_not_awaited()


def test_enrich_tick_dos_upserts_con_y_sin_deltas(steam_api, repos, client):
    names = [AK["markethashname"], KNIFE["markethashname"]]
    repos["fetch_stalest"].return_value = names
    resp = client.post("/internal/enrich-tick", headers=TOKEN)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "count": 2, "with_deltas": 1}
    repos["fetch_stalest"].assert_awaited_once_with(18)

    con, sin = (c.args[0] for c in repos["upsert_rows"].await_args_list)
    (fila_con,) = con
    assert set(fila_con) == {"name", "price_delta_24h", "price_delta_7d", "price_delta_30d", "enriched_at"}
    assert (fila_con["name"], fila_con["price_delta_7d"]) == (names[0], 10.0)
    # Sin histórico solo se marca como intentada: escribir los deltas a null pisaría
    # los de _inline_delta.
    (fila_sin,) = sin
    assert set(fila_sin) == {"name", "enriched_at"} and fila_sin["name"] == names[1]
    assert fila_sin["enriched_at"] == fila_con["enriched_at"]
