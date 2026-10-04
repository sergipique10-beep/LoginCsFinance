"""SEC-16 (punto 4): un 402 de steamwebapi es cuota MENSUAL agotada, no «vas demasiado
rápido». Antes salía como 429 y el front decía «Demasiadas peticiones» al abrir un
detalle de skin sin caché. Ahora: caché caducada si la hay; si no, 503 con un código
estable (`upstream_quota`) que el front traduce a su propio aviso."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
from stores import _item_price_cache, _market_index_cache, _market_prices_cache, _search_cache

STALE = -1e9   # timestamp monotónico muy antiguo: la entrada existe pero ha caducado

# (url, caché, clave) de cada ruta que llama a steamwebapi y puede recibir un 402.
ROUTES = [
    ("/market/items?q=AK", _search_cache, "ak", []),
    ("/market/price?name=AK", _item_price_cache, "ak", {"name": "AK"}),
    ("/market/index?tf=24h", _market_index_cache, "24h", {"points": []}),
    ("/market/prices?market=buff&name=AK", _market_prices_cache, "buff:ak:usd", {"price": 1}),
]


@pytest.fixture
def steam_402(client, monkeypatch):
    http = MagicMock()
    http.get = AsyncMock(return_value=MagicMock(status_code=402))
    http.aclose = AsyncMock()
    monkeypatch.setattr(main.app.state, "http_client", http)
    for _, cache, _, _ in ROUTES:
        cache.clear()
    yield client
    for _, cache, _, _ in ROUTES:
        cache.clear()


@pytest.mark.parametrize("url,cache,key,value", ROUTES)
def test_402_without_cache_is_503_upstream_quota(steam_402, url, cache, key, value):
    resp = steam_402.get(url)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "upstream_quota"


@pytest.mark.parametrize("url,cache,key,value", ROUTES)
def test_402_serves_stale_cache_instead_of_error(steam_402, url, cache, key, value):
    cache[key] = (value, STALE)
    resp = steam_402.get(url)
    assert resp.status_code == 200
    assert resp.json() == value
