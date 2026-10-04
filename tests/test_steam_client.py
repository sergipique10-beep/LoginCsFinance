"""CLEAN-06: cliente único de steamwebapi y errores tipados.

Cada respuesta de steamwebapi se traduce a un error tipado en un solo sitio
(`steam/clients/steamwebapi.py`). Los llamadores deciden qué HTTP devolver.
"""
import httpx
import pytest

from steam.clients import steamwebapi
from steam.errors import (
    InvalidPayload, QuotaExhausted, RateLimited, SourceTimeout, SourceUnavailable, UpstreamError,
)


def _client(status=200, *, json=None, content=None, headers=None, exc=None, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if exc is not None:
            raise exc
        if content is not None:
            return httpx.Response(status, content=content, headers=headers)
        return httpx.Response(status, json=json, headers=headers)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_200_devuelve_el_json_tal_cual():
    async with _client(json=[{"a": 1}]) as c:
        assert await steamwebapi.profile(c, "1") == [{"a": 1}]


@pytest.mark.parametrize("status, error", [
    (402, QuotaExhausted), (403, UpstreamError), (404, UpstreamError), (410, UpstreamError),
    (411, UpstreamError), (429, RateLimited), (500, UpstreamError),
])
async def test_status_no_200_es_un_error_tipado(status, error):
    async with _client(status, content=b"cuerpo") as c:
        with pytest.raises(error) as info:
            await steamwebapi.inventory(c, "1")
    assert type(info.value) is error
    assert info.value.status == status
    assert info.value.body_excerpt == "cuerpo"


@pytest.mark.parametrize("headers, retry_after", [
    ({"Retry-After": "7"}, 7.0), ({}, None), ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
])
async def test_429_parsea_retry_after(headers, retry_after):
    async with _client(429, headers=headers) as c:
        with pytest.raises(RateLimited) as info:
            await steamwebapi.items(c, select="id", max=1, search="x")
    assert info.value.retry_after == retry_after


@pytest.mark.parametrize("exc, error", [
    (httpx.ReadTimeout("lento"), SourceTimeout), (httpx.ConnectError("caído"), SourceUnavailable),
])
async def test_red(exc, error):
    async with _client(exc=exc) as c:
        with pytest.raises(error) as info:
            await steamwebapi.market_index(c)
    # El texto es el de httpx: las rutas lo ponen en el detail («Could not reach Steam: …»).
    assert str(info.value) == str(exc)
    assert isinstance(info.value, UpstreamError)


async def test_json_invalido_no_es_un_upstream_error():
    # Hoy un 200 ilegible da 500 en todos los llamadores (CAL-14). Si heredara de
    # UpstreamError, cualquier `except UpstreamError` lo convertiría en 502 sin querer.
    async with _client(content=b"<html>") as c:
        with pytest.raises(InvalidPayload) as info:
            await steamwebapi.item(c, "AK")
    assert not isinstance(info.value, UpstreamError)
    assert info.value.body_excerpt == "<html>"


async def test_cada_endpoint_url_parametros_y_timeout():
    seen: list[httpx.Request] = []
    async with _client(json=[], seen=seen) as c:
        await steamwebapi.items(c, select="id,name", max=5, search="ak")
        await steamwebapi.items(c, select="id", max=10, sort_by="soldZa")
        await steamwebapi.item(c, "AK")
        await steamwebapi.inventory(c, "765")
        await steamwebapi.profile(c, "765")
        await steamwebapi.market_index(c, timeout=15.0)
        await steamwebapi.market_prices(c, "buff", {"format": "json"}, timeout=30.0)
        await steamwebapi.market_history(c, "csfloat", "AK", "2026-09-01", "2026-10-01", timeout=30.0)
        await steamwebapi.legacy_history(c, "AK", "10")
        await steamwebapi.info_markets(c)

    got = [(r.url.path, dict(r.url.params), r.extensions["timeout"]["read"]) for r in seen]
    assert got == [
        ("/steam/api/items", {"game": "cs2", "search": "ak", "max": "5", "select": "id,name",
                              "format": "json", "production": "1"}, 15.0),
        ("/steam/api/items", {"game": "cs2", "sort_by": "soldZa", "max": "10", "select": "id",
                              "format": "json", "production": "1"}, 15.0),
        ("/steam/api/item", {"game": "cs2", "market_hash_name": "AK", "format": "json"}, 20.0),
        ("/steam/api/inventory", {"steam_id": "765", "game": "cs2", "language": "english",
                                  "limit": "5000", "no_cache": "1"}, 5.0),
        ("/steam/api/profile", {"id": "765"}, 5.0),
        ("/steam/api/market-index/cs2", {"format": "json"}, 15.0),
        ("/market/buff/prices", {"format": "json"}, 30.0),
        ("/market/csfloat/history", {"market_hash_name": "AK", "start_date": "2026-09-01",
                                     "end_date": "2026-10-01"}, 30.0),
        ("/steam/api/history", {"market_hash_name": "AK", "interval": "10", "format": "json"}, 5.0),
        ("/steam/api/info/markets", {}, 15.0),
    ]
    assert all("X-Api-Key" in r.headers for r in seen)


async def test_item_sigue_redirecciones():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/item"):
            return httpx.Response(301, headers={"Location": "https://www.steamwebapi.com/steam/api/item2"})
        return httpx.Response(200, json={"ok": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await steamwebapi.item(c, "AK") == {"ok": 1}
    assert len(seen) == 2

