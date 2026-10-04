"""PERF-17: los lookups de precios por mercado y de proveedores apuntan el fallo y no
reintentan durante _LOOKUP_FAIL_TTL. Durante el backoff se sigue sirviendo el último
dato bueno (stale-on-error); el caché negativo no lo pisa."""
import time
from unittest.mock import MagicMock

import httpx
import pytest

from steam import services
from stores import _lookup_failed_at, _market_lookup_cache, _market_providers_cache


class _FakeClient:
    def __init__(self, status: int = 500, payload=None, exc: Exception | None = None):
        self.calls = 0
        self.status = status
        self.payload = payload
        self.exc = exc

    async def get(self, url, **kwargs):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return MagicMock(status_code=self.status, json=lambda: self.payload)


@pytest.fixture(autouse=True)
def _clean():
    stores = (_market_lookup_cache, _market_providers_cache, _lookup_failed_at)
    saved = [dict(s) for s in stores]
    for s in stores:
        s.clear()
    yield
    for s, old in zip(stores, saved, strict=True):
        s.clear()
        s.update(old)


def _expire_backoff(key: str) -> None:
    _lookup_failed_at[key] = time.monotonic() - services._LOOKUP_FAIL_TTL - 1


# ── _fetch_market_price_lookup ────────────────────────────────────────────────

@pytest.mark.parametrize("client", [
    _FakeClient(status=500),
    _FakeClient(status=200, payload={"error": "x"}),          # formato inesperado
    _FakeClient(exc=httpx.ReadTimeout("timeout")),
])
async def test_price_lookup_failure_is_cached_then_retried(client):
    assert await services._fetch_market_price_lookup(client, "csfloat") == {}
    assert await services._fetch_market_price_lookup(client, "csfloat") == {}
    assert client.calls == 1                                   # el fallo se apunta

    _expire_backoff("lookup:csfloat")
    await services._fetch_market_price_lookup(client, "csfloat")
    assert client.calls == 2                                   # pasado el backoff, reintenta


async def test_price_lookup_backoff_is_per_market():
    client = _FakeClient(status=500)
    await services._fetch_market_price_lookup(client, "csfloat")
    await services._fetch_market_price_lookup(client, "buff")
    assert client.calls == 2


async def test_price_lookup_serves_last_good_during_backoff():
    good = _FakeClient(status=200, payload=[{"market_hash_name": "AK", "price": 10}])
    assert await services._fetch_market_price_lookup(good, "csfloat") == {"AK": 10.0}
    # El dato bueno caduca y la fuente cae.
    lookup, _ = _market_lookup_cache["csfloat"]
    _market_lookup_cache["csfloat"] = (lookup, time.monotonic() - services.MARKET_LOOKUP_CACHE_TTL - 1)

    down = _FakeClient(status=500)
    assert await services._fetch_market_price_lookup(down, "csfloat") == {"AK": 10.0}
    assert await services._fetch_market_price_lookup(down, "csfloat") == {"AK": 10.0}
    assert down.calls == 1
    assert _market_lookup_cache["csfloat"][0] == {"AK": 10.0}  # el negativo no pisa el bueno


async def test_price_lookup_success_clears_failure_mark():
    await services._fetch_market_price_lookup(_FakeClient(status=500), "csfloat")
    _expire_backoff("lookup:csfloat")
    good = _FakeClient(status=200, payload=[{"market_hash_name": "AK", "price": 10}])
    await services._fetch_market_price_lookup(good, "csfloat")
    assert "lookup:csfloat" not in _lookup_failed_at


# ── _fetch_market_providers ───────────────────────────────────────────────────

async def test_providers_failure_is_cached_then_retried():
    client = _FakeClient(status=500)
    assert await services._fetch_market_providers(client) == services._FALLBACK_PROVIDERS
    assert await services._fetch_market_providers(client) == services._FALLBACK_PROVIDERS
    assert client.calls == 1

    _expire_backoff("providers")
    await services._fetch_market_providers(client)
    assert client.calls == 2


async def test_providers_serve_last_good_during_backoff():
    good = _FakeClient(status=200, payload=[{"id": "csfloat", "name": "CSFloat X", "logo": "https://l/x.png"}])
    providers = await services._fetch_market_providers(good)
    assert providers[1]["name"] == "CSFloat X"
    _market_providers_cache["providers"] = (providers, time.monotonic() - services.MARKET_PROVIDERS_CACHE_TTL - 1)

    down = _FakeClient(exc=httpx.ConnectError("down"))
    assert await services._fetch_market_providers(down) == providers
    assert await services._fetch_market_providers(down) == providers
    assert down.calls == 1
