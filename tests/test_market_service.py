"""CLEAN-15: status y reason de los `Fetched` de `steam/services/market_service.py` ante una
fuente caída, 402, 429 y payload corrupto. El contrato HTTP lo fijan
tests/test_steam_contract_*; aquí se mira lo que ve la ruta antes de traducir."""
import httpx
import pytest

from steam.errors import InvalidPayload, QuotaExhausted, RateLimited, SourceTimeout, UnexpectedPayload
from steam.services import market_service as market_service
from stores import _item_price_cache, _market_index_cache, _search_cache, _topmovers_raw_cache
from tests.test_steam_contract_market import NAME, RAW

TOPMOVERS = {"topmovers": {"gainers": [{"markethashname": "G", "price": 1, "change24h": 5}], "losers": []}}


@pytest.fixture
def http(steam_api):
    import main
    return main.app.state.http_client


# ── rankings ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("compute", [market_service.compute_movers, market_service.compute_trending])
@pytest.mark.parametrize("route, reason", [
    ({"status": 500}, "http_500"), ({"status": 402}, "quota"), ({"status": 429}, "rate_limit"),
    ({"exc": httpx.ReadTimeout("t")}, "timeout"), ({"content": b"<html>"}, "invalid_json"),
    ({"json": {"no": "lista"}}, "unexpected_format"),
])
async def test_rankings_caen_a_topmovers_con_el_motivo(steam_api, http, compute, route, reason):
    steam_api.on("api/items", **route)
    steam_api.on("market-index/cs2", json=TOPMOVERS)
    fetched = await compute(http)
    assert (fetched.status, fetched.reason) == ("partial", reason)
    assert fetched.data


@pytest.mark.parametrize("compute, vacio", [
    (market_service.compute_movers, {"hot": [], "cold": []}), (market_service.compute_trending, []),
])
async def test_rankings_sin_fuentes(steam_api, http, compute, vacio):
    steam_api.on("api/items", status=500)
    steam_api.on("market-index/cs2", content=b"<html>")
    fetched = await compute(http)
    assert fetched == market_service.Fetched(vacio, "error", "http_500")


async def test_topmovers_fresco_se_reutiliza_y_caducado_no(steam_api, http):
    steam_api.on("market-index/cs2", json=TOPMOVERS)
    assert (await market_service.get_market_index(http, "24h")).status == "ok"
    steam_api.on("api/items", status=500)
    steam_api.on("market-index/cs2", status=500)
    assert (await market_service.compute_movers(http)).status == "partial"   # cache fresca
    _topmovers_raw_cache.put("latest", _topmovers_raw_cache.stale("latest"), now=-1e9)
    fetched = await market_service.compute_movers(http)
    assert (fetched.status, fetched.reason) == ("error", "topmovers_stale")   # CAL-12


# ── búsqueda, item e índice ───────────────────────────────────────────────────

@pytest.mark.parametrize("fn, cache, key", [
    (market_service.search_market, _search_cache, f"market:{NAME.lower()}"),
    (market_service.get_item_full, _item_price_cache, NAME.lower()),
])
async def test_402_stale_o_sube(steam_api, http, fn, cache, key):
    steam_api.on("api/items", status=402)
    with pytest.raises(QuotaExhausted):
        await fn(http, NAME)
    cache.put(key, [{"name": NAME}] if cache is _search_cache else {"name": NAME}, now=-1e9)
    fetched = await fn(http, NAME)
    assert (fetched.status, fetched.reason) == ("stale", "quota")


@pytest.mark.parametrize("route, exc", [
    ({"status": 429}, RateLimited), ({"exc": httpx.ReadTimeout("t")}, SourceTimeout),
    ({"content": b"<html>"}, InvalidPayload), ({"json": {"no": "lista"}}, UnexpectedPayload),
])
async def test_busqueda_fallos_tipados_sin_cachear(steam_api, http, route, exc):
    steam_api.on("api/items", **route)
    with pytest.raises(exc):
        await market_service.search_market(http, NAME)
    assert not _search_cache


async def test_busqueda_ok_con_namespace(steam_api, http):
    steam_api.on("api/items", json=[RAW])
    fetched = await market_service.search_market(http, NAME)
    assert fetched.status == "ok" and [i["name"] for i in fetched.data] == [NAME]
    assert set(_search_cache) == {f"market:{NAME.lower()}"}   # CAL-11


async def test_indice_402_stale_e_invalid_payload(steam_api, http):
    _market_index_cache.put("24h", {"x": 1}, now=-1e9)
    steam_api.on("market-index/cs2", status=402)
    fetched = await market_service.get_market_index(http, "24h")
    assert (fetched.data, fetched.status, fetched.reason) == ({"x": 1}, "stale", "quota")
    steam_api.on("market-index/cs2", content=b"<html>")
    with pytest.raises(InvalidPayload):
        await market_service.get_market_index(http, "7d")
