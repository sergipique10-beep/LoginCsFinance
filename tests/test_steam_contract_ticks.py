"""CAL-09 (Fase 0): contrato de los ticks de mercado (cap, trending, enrich, movers) y de
los rankings que calculan, con steamwebapi simulado por HTTP (fixture `steam_api`) y
los repos de Supabase sustituidos.

Se congela la respuesta de cada tick y QUÉ items acaban en cada tabla y en qué orden
(filtros de precio, ventas y slab, orden por turnover, fallback de topmovers en trending;
rankings de Home en movers). Durante STEAM-REFACTOR estos tests no se tocan; los de movers
cambiaron a propósito con los rankings de Home (docs/superpowers/specs/2026-11-03-home-rankings-design.md).
"""
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

def _home(name, *, real=None, real30=None, sold7=None, sold24=None, median=None):
    return {"markethashname": name, "pricereal": real, "pricereal30d": real30, "sold7d": sold7,
            "sold24h": sold24, "pricemedian": median, "pricelatestsell": 999.0}


def test_movers_tick_desde_items(steam_api, repos, client):
    """Rankings de Home: cajas por ventas 24 h; suben/bajan en $ a 30 días, sin relleno."""
    dn = _home("Dreams & Nightmares Case", sold24=1000, median=1.4)
    kw = _home("Kilowatt Case", sold24=900, median=0.12)
    ak = _home("AK-47 | Redline (Field-Tested)", real=30, real30=20, sold7=50, median=33)      # +10 $
    knife = _home("★ Karambit | Doppler (Factory New)", real=300, real30=250, sold7=6, median=320)  # +50 $
    awp = _home("AWP | Asiimov (Field-Tested)", real=50, real30=60, sold7=10, median=55)        # -10 $
    flat = _home("Glock-18 | Fade (Factory New)", real=15, real30=15, sold7=20)                # 0 → fuera
    thin = _home("M4A4 | Howl (Field-Tested)", real=100, real30=50, sold7=4)                   # < 5 ventas
    slab = _home("Sticker Slab | Crown (Foil)", real=40, real30=20, sold7=50)                  # slab
    steam_api.on("api/items", json=[dn, kw, ak, knife, awp, flat, thin, slab])

    resp = client.post("/internal/movers-tick", headers=TOKEN)
    assert resp.json() == {"ok": True, "count": 5}

    rows = _rows(repos["replace_snapshot"])
    por_bucket = {b: [(r["name"], r["rank"]) for r in rows if r["bucket"] == b] for b in ("cases", "up", "down")}
    assert por_bucket == {
        "cases": [(dn["markethashname"], 0), (kw["markethashname"], 1)],
        "up": [(knife["markethashname"], 0), (ak["markethashname"], 1)],
        "down": [(awp["markethashname"], 0)],
    }
    # Precio = mediana de Steam, nunca la última venta (999 en el payload).
    assert {r["name"]: r["price_latest"] for r in rows}[knife["markethashname"]] == 320
    # Dos peticiones a /items (la segunda, las caras) y ninguna a csfloat/history.
    assert [r.url.params.get("price_real_min") for r in steam_api.hits("api/items")] == [None, "50"]
    assert len(steam_api.hits("csfloat/history")) == 0


@pytest.mark.parametrize("items_route", [{"status": 500}, {"exc": TIMEOUT}, {"json": {"error": "x"}}])
def test_movers_tick_sin_items_conserva_el_snapshot(steam_api, repos, client, items_route):
    # Sin respaldo de topmovers (da % en 24 h, no encaja con estas listas): CAL-10.
    steam_api.on("api/items", **items_route)
    steam_api.on("market-index/cs2", json=TOPMOVERS)
    resp = client.post("/internal/movers-tick", headers=TOKEN)
    assert resp.json() == {"ok": False, "count": 0, "kept_previous": True}
    repos["replace_snapshot"].assert_not_awaited()
    assert steam_api.hits("market-index/cs2") == []


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
