"""CLEAN-07: clientes de las fuentes externas que no son steamwebapi (catálogo de
ByMykel, frankfurter, Steam News y og:image). Cada uno devuelve el JSON o lanza el
error tipado de `steam/errors.py`; la lógica (caché, backoff, validación) se queda
en quien llama.
"""
import ast
from pathlib import Path

import httpx
import pytest

from steam.clients import fx, static_catalog, steam_news
from steam.errors import InvalidPayload, SourceTimeout, SourceUnavailable, UpstreamError

ROOT = Path(__file__).resolve().parent.parent


def _client(status=200, *, json=None, content=None, exc=None, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if exc is not None:
            raise exc
        if content is not None:
            return httpx.Response(status, content=content)
        return httpx.Response(status, json=json)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── catálogo estático ─────────────────────────────────────────────────────────

async def test_catalogo_devuelve_la_lista():
    seen: list[httpx.Request] = []
    async with _client(json=[{"name": "x"}], seen=seen) as c:
        assert await static_catalog.fetch_source(c, "https://raw.githubusercontent.com/a.json") == [{"name": "x"}]
    assert seen[0].extensions["timeout"]["read"] == 15.0
    assert "X-Api-Key" not in seen[0].headers   # la clave de steamwebapi no sale de su cliente


@pytest.mark.parametrize("kwargs, error", [
    ({"status": 500}, UpstreamError),
    ({"json": {"no": "lista"}}, InvalidPayload),
    ({"content": b"<html>"}, InvalidPayload),
    ({"exc": httpx.ReadTimeout("t")}, SourceTimeout),
    ({"exc": httpx.ConnectError("x")}, SourceUnavailable),
])
async def test_catalogo_errores(kwargs, error):
    async with _client(**kwargs) as c:
        with pytest.raises(error):
            await static_catalog.fetch_source(c, "https://raw.githubusercontent.com/a.json")


# ── FX ────────────────────────────────────────────────────────────────────────

async def test_fx_pide_usd_eur_a_frankfurter():
    seen: list[httpx.Request] = []
    async with _client(json={"rates": {"EUR": 0.9}}, seen=seen) as c:
        assert await fx.latest_usd_eur(c) == {"rates": {"EUR": 0.9}}
    (req,) = seen
    assert req.url.host == "api.frankfurter.dev"
    assert dict(req.url.params) == {"base": "USD", "symbols": "EUR"}
    assert req.extensions["timeout"]["read"] == 10.0


async def test_fx_status_no_200():
    async with _client(503) as c:
        with pytest.raises(UpstreamError) as info:
            await fx.latest_usd_eur(c)
    assert info.value.status == 503


# ── Steam News ────────────────────────────────────────────────────────────────

async def test_news_pide_a_steam_con_el_timeout_del_cliente():
    seen: list[httpx.Request] = []
    async with _client(json={"appnews": {}}, seen=seen) as c:
        assert await steam_news.get_news(c, 15) == {"appnews": {}}
    (req,) = seen
    assert req.url.path == "/ISteamNews/GetNewsForApp/v2/"
    assert dict(req.url.params) == {"appid": "730", "count": "15", "format": "json"}
    assert req.extensions["timeout"]["read"] == 5.0   # el por defecto de este cliente de prueba


@pytest.mark.parametrize("kwargs, error", [
    ({"status": 500}, UpstreamError),
    ({"exc": httpx.ReadTimeout("t")}, SourceTimeout),
    ({"exc": httpx.ConnectError("x")}, SourceUnavailable),
])
async def test_news_errores(kwargs, error):
    async with _client(**kwargs) as c:
        with pytest.raises(error):
            await steam_news.get_news(c, 5)


async def test_og_image_timeout_y_redirecciones():
    seen: list[httpx.Request] = []
    async with _client(content=b'<meta property="og:image" content="https://img/a.jpg">', seen=seen) as c:
        assert await steam_news.fetch_og_image(c, "https://news/x") == "https://img/a.jpg"
    assert seen[0].extensions["timeout"]["read"] == 4.0
    assert seen[0].headers["User-Agent"] == "Mozilla/5.0"


# ── Mappers sin HTTP ──────────────────────────────────────────────────────────

def test_mappers_no_importa_httpx():
    # CLEAN-08 partió steam/mappers.py en el paquete steam/mappers/: la guardia lo cubre entero.
    files = sorted((ROOT / "steam" / "mappers").glob("*.py"))
    assert files
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "httpx" not in imported, path.name
