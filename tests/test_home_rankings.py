"""Rankings de Home: cajas más vendidas y skins que más suben/bajan en $ a 30 días."""
import pytest

from steam.adapters.steam_adapter import adapt_item
from steam.domain.home_rankings import (
    HOME_RANKING_LIMIT, change_30d, is_case, merge_universe, top_cases, top_movers, usd_from_pct,
)


def _item(name, *, real=None, real30=None, sold7=None, sold24=None):
    return adapt_item({"markethashname": name, "pricereal": real, "pricereal30d": real30,
                       "sold7d": sold7, "sold24h": sold24})


# ── reglas puras ──────────────────────────────────────────────────────────────

def test_is_case():
    assert is_case(_item("Dreams & Nightmares Case"))
    assert not is_case(_item("Sticker Capsule"))
    assert not is_case(_item("AK-47 | Case Hardened (Field-Tested)"))


def test_universo_sin_duplicados():
    a, b = _item("X", real=10), _item("X", real=99)
    assert [i.price_real for i in merge_universe([a], [b, _item("Y")])] == [10, None]


def test_cambio_30d():
    usd, pct = change_30d(_item("K", real=1072.82, real30=1175.0))
    assert round(usd, 2) == -102.18 and round(pct, 1) == -8.7


@pytest.mark.parametrize("real, real30", [(100, None), (100, 0), (None, 50), (148.35, 1.83)])
def test_cambio_30d_descarta_basura(real, real30):
    assert change_30d(_item("G", real=real, real30=real30)) is None


def test_top_cases_por_unidades_sin_ceros():
    items = [_item("A Case", sold24=5), _item("B Case", sold24=110497), _item("C Case", sold24=0),
             _item("AK | X", sold24=9999)]
    assert [i.name for i in top_cases(items)] == ["B Case", "A Case"]


def test_top_movers_dolares_sin_relleno_y_minimo_ventas():
    items = [
        _item("Knife", real=750.95, real30=863.06, sold7=10),   # -112.11
        _item("Gloves", real=96.80, real30=62.64, sold7=11),    # +34.16
        _item("Flat", real=5, real30=5, sold7=50),              # 0 → fuera
        _item("Thin", real=600, real30=300, sold7=4),           # < 5 ventas → fuera
        _item("Big Case", real=10, real30=5, sold7=99),         # caja → fuera
    ]
    assert [i.name for i, *_ in top_movers(items, "up")] == ["Gloves"]
    assert [i.name for i, *_ in top_movers(items, "down")] == ["Knife"]


def test_top_movers_limite():
    items = [_item(f"S{n}", real=100 + n, real30=100, sold7=5) for n in range(1, 15)]
    assert len(top_movers(items, "up")) == HOME_RANKING_LIMIT
    assert top_movers(items, "up")[0][0].name == "S14"


def test_usd_from_pct():
    _, pct = change_30d(_item("G", real=96.80, real30=62.64))
    assert round(usd_from_pct(96.80, pct), 2) == 34.16
    assert usd_from_pct(None, 5) is None and usd_from_pct(10, None) is None


# ── cálculo en el movers-tick (dos peticiones a /items) ───────────────────────

import httpx  # noqa: E402

from steam.domain.models import Fetched  # noqa: E402
from steam.services import rankings_service  # noqa: E402

CASE = {"markethashname": "Dreams & Nightmares Case", "pricemedian": 1.4, "sold24h": 110497, "sold7d": 700000}
RISER = {"markethashname": "★ Moto Gloves | 3rd Commando Company (Minimal Wear)", "pricemedian": 120.0,
         "pricereal": 96.80, "pricereal30d": 62.64, "sold7d": 11, "sold24h": 2}
FALLER = {"markethashname": "★ Karambit | Doppler (Factory New)", "pricemedian": 1300.0,
          "pricereal": 1072.82, "pricereal30d": 1175.0, "sold7d": 15, "sold24h": 3}


@pytest.fixture
def http(steam_api):
    import main
    return main.app.state.http_client


def _items_route(steam_api, *, cheap, expensive):
    """`cheap`/`expensive`: lista JSON o un status de error para cada petición."""
    def fn(request: httpx.Request) -> httpx.Response:
        body = expensive if "price_real_min" in request.url.params else cheap
        return httpx.Response(body, json={}) if isinstance(body, int) else httpx.Response(200, json=body)
    steam_api.on("api/items", fn=fn)


async def test_compute_dos_peticiones_y_tres_listas(steam_api, http):
    _items_route(steam_api, cheap=[CASE, RISER], expensive=[FALLER, RISER])
    fetched = await rankings_service.compute_home_rankings(http)
    assert fetched.status == "ok"
    assert set(fetched.data) == {"cases", "up", "down"}
    assert [c["name"] for c in fetched.data["cases"]] == [CASE["markethashname"]]
    assert [c["name"] for c in fetched.data["up"]] == [RISER["markethashname"]]
    assert [c["name"] for c in fetched.data["down"]] == [FALLER["markethashname"]]
    assert round(fetched.data["down"][0]["priceChange30d"], 2) == -102.18
    params = [dict(r.url.params) for r in steam_api.hits("api/items")]
    assert sorted(p.get("price_real_min") for p in params if "price_real_min" in p) == ["50"]


async def test_compute_usa_mediana_no_ultima_venta(steam_api, http):
    bizon = {**RISER, "pricelatestsell": 342.12, "pricemedian": 0.03}
    _items_route(steam_api, cheap=[bizon], expensive=[])
    fetched = await rankings_service.compute_home_rankings(http)
    assert fetched.data["up"][0]["priceLatest"] == 0.03


async def test_compute_caras_fallan_es_partial(steam_api, http):
    _items_route(steam_api, cheap=[CASE, RISER], expensive=500)
    fetched = await rankings_service.compute_home_rankings(http)
    assert (fetched.status, fetched.reason) == ("partial", "http_500")
    assert fetched.data["cases"] and fetched.data["up"]


async def test_compute_baratas_fallan_es_error(steam_api, http):
    _items_route(steam_api, cheap=500, expensive=[FALLER])
    fetched = await rankings_service.compute_home_rankings(http)
    assert fetched == Fetched({"cases": [], "up": [], "down": []}, "error", "http_500")


# ── lectura: GET /market/movers ───────────────────────────────────────────────

from unittest.mock import AsyncMock  # noqa: E402

from steam.mappers.row_mapper import _to_row  # noqa: E402
from steam.rankings_repo import movers_repo  # noqa: E402


def test_movers_ignora_buckets_viejos(client, monkeypatch):
    # Justo tras el deploy, el snapshot aún es HOT/COLD hasta el primer movers-tick.
    rows = [{"name": "X", "bucket": "hot", "rank": 0, "price_latest": 1.0},
            {"name": "Y", "bucket": "cold", "rank": 0, "price_latest": 1.0}]
    monkeypatch.setattr(movers_repo, "fetch_snapshot", AsyncMock(return_value=rows))
    resp = client.get("/market/movers")
    assert resp.status_code == 200
    assert resp.json() == {"cases": [], "up": [], "down": [], "hot": [], "cold": []}


def test_movers_cambio_en_dolares(client, monkeypatch):
    _, pct = change_30d(_item("G", real=96.80, real30=62.64))
    card = {"name": "G", "priceReal": 96.80, "priceDelta30d": pct, "priceLatest": 120.0}
    monkeypatch.setattr(movers_repo, "fetch_snapshot", AsyncMock(return_value=[_to_row(card, 0, "up")]))
    up = client.get("/market/movers").json()["up"]
    assert round(up[0]["priceChange30d"], 2) == 34.16


# ── revisión final: falla solo la petición de las caras ───────────────────────

from steam.routes import market as market_routes  # noqa: E402


def test_tick_partial_conserva_suben_y_bajan_anteriores(client, monkeypatch):
    """Sin las caras, suben/bajan se quedarían sin cuchillos ni guantes y sin aviso: el tick
    renueva solo las cajas y conserva las listas anteriores, y lo dice en su respuesta."""
    monkeypatch.setattr(market_routes, "CAP_TICK_TOKEN", "secret123")
    previo = [_to_row({"name": "Karambit", "priceLatest": 1300}, 0, "down"),
              _to_row({"name": "Moto Gloves", "priceLatest": 120}, 0, "up"),
              _to_row({"name": "Old Case", "priceLatest": 1}, 0, "cases")]
    monkeypatch.setattr(movers_repo, "fetch_snapshot", AsyncMock(return_value=previo))
    replace = AsyncMock()
    monkeypatch.setattr(movers_repo, "replace_snapshot", replace)
    nuevo = {"cases": [{"name": "Dreams & Nightmares Case", "priceLatest": 1.4}],
             "up": [{"name": "Cheap Riser", "priceLatest": 2}], "down": []}
    monkeypatch.setattr(rankings_service, "compute_home_rankings",
                        AsyncMock(return_value=Fetched(nuevo, "partial", "http_500")))

    resp = client.post("/internal/movers-tick", headers={"X-Cap-Token": "secret123"})

    assert resp.json() == {"ok": True, "count": 3, "status": "partial", "reason": "http_500"}
    rows = replace.await_args.args[0]
    assert sorted((r["bucket"], r["name"]) for r in rows) == [
        ("cases", "Dreams & Nightmares Case"), ("down", "Karambit"), ("up", "Moto Gloves")]


def test_movers_alias_hot_cold_para_apps_ya_instaladas(client, monkeypatch):
    """Transición: la app publicada lee `hot`/`cold`; sin alias su Home quedaría vacía."""
    rows = [_to_row({"name": "U", "priceLatest": 1}, 0, "up"), _to_row({"name": "D", "priceLatest": 1}, 0, "down")]
    monkeypatch.setattr(movers_repo, "fetch_snapshot", AsyncMock(return_value=rows))
    body = client.get("/market/movers").json()
    assert body["hot"] == body["up"] and [c["name"] for c in body["hot"]] == ["U"]
    assert body["cold"] == body["down"] and [c["name"] for c in body["cold"]] == ["D"]
