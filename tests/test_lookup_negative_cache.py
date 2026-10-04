"""PERF-17: los lookups de precios por mercado y de proveedores apuntan el fallo y no
reintentan durante _LOOKUP_FAIL_TTL. Durante el backoff se sigue sirviendo el último
dato bueno (stale-on-error); el caché negativo no lo pisa."""
import time
from unittest.mock import MagicMock

import httpx
import pytest

from steam.services import pricing
from steam.services import providers as providers_service
from steam.domain import catalog
from stores import (
    LOOKUP_FAIL_TTL, MARKET_LOOKUP_CACHE_TTL, MARKET_PROVIDERS_CACHE_TTL,
    _market_lookup_cache, _market_providers_cache,
)


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
    stores = (_market_lookup_cache, _market_providers_cache)
    saved = [dict(s) for s in stores]
    for s in stores:
        s.clear()
    yield
    for s, old in zip(stores, saved, strict=True):
        s.clear()
        s.update(old)


def _expire_backoff(key: str) -> None:
    # CLEAN-09: el backoff vive en cada caché ("lookup:<market>" → _market_lookup_cache).
    cache, k = ((_market_lookup_cache, key.split(":", 1)[1]) if key.startswith("lookup:")
                else (_market_providers_cache, key))
    cache.mark_failed(k, now=time.monotonic() - LOOKUP_FAIL_TTL - 1)


# ── _fetch_market_price_lookup ────────────────────────────────────────────────

@pytest.mark.parametrize("client", [
    _FakeClient(status=500),
    _FakeClient(status=200, payload={"error": "x"}),          # formato inesperado
    _FakeClient(exc=httpx.ReadTimeout("timeout")),
])
async def test_price_lookup_failure_is_cached_then_retried(client):
    assert await pricing._fetch_market_price_lookup(client, "csfloat") == {}
    assert await pricing._fetch_market_price_lookup(client, "csfloat") == {}
    assert client.calls == 1                                   # el fallo se apunta

    _expire_backoff("lookup:csfloat")
    await pricing._fetch_market_price_lookup(client, "csfloat")
    assert client.calls == 2                                   # pasado el backoff, reintenta


async def test_price_lookup_backoff_is_per_market():
    client = _FakeClient(status=500)
    await pricing._fetch_market_price_lookup(client, "csfloat")
    await pricing._fetch_market_price_lookup(client, "buff")
    assert client.calls == 2


async def test_price_lookup_serves_last_good_during_backoff():
    good = _FakeClient(status=200, payload=[{"market_hash_name": "AK", "price": 10}])
    assert await pricing._fetch_market_price_lookup(good, "csfloat") == {"AK": 10.0}
    # El dato bueno caduca y la fuente cae.
    lookup, _ = _market_lookup_cache["csfloat"]
    _market_lookup_cache["csfloat"] = (lookup, time.monotonic() - MARKET_LOOKUP_CACHE_TTL - 1)

    down = _FakeClient(status=500)
    assert await pricing._fetch_market_price_lookup(down, "csfloat") == {"AK": 10.0}
    assert await pricing._fetch_market_price_lookup(down, "csfloat") == {"AK": 10.0}
    assert down.calls == 1
    assert _market_lookup_cache["csfloat"][0] == {"AK": 10.0}  # el negativo no pisa el bueno


async def test_price_lookup_success_clears_failure_mark():
    await pricing._fetch_market_price_lookup(_FakeClient(status=500), "csfloat")
    _expire_backoff("lookup:csfloat")
    good = _FakeClient(status=200, payload=[{"market_hash_name": "AK", "price": 10}])
    await pricing._fetch_market_price_lookup(good, "csfloat")
    assert not _market_lookup_cache.in_backoff("csfloat")


# ── _fetch_market_providers ───────────────────────────────────────────────────

async def test_providers_failure_is_cached_then_retried():
    client = _FakeClient(status=500)
    assert (await providers_service.fetch_market_providers(client)).data == catalog.fallback_providers()
    assert (await providers_service.fetch_market_providers(client)).data == catalog.fallback_providers()
    assert client.calls == 1

    _expire_backoff("providers")
    await providers_service.fetch_market_providers(client)
    assert client.calls == 2


async def test_providers_serve_last_good_during_backoff():
    good = _FakeClient(status=200, payload=[{"id": "csfloat", "name": "CSFloat X", "logo": "https://l/x.png"}])
    providers = (await providers_service.fetch_market_providers(good)).data
    assert providers[1]["name"] == "CSFloat X"
    _market_providers_cache["providers"] = (providers, time.monotonic() - MARKET_PROVIDERS_CACHE_TTL - 1)

    down = _FakeClient(exc=httpx.ConnectError("down"))
    assert (await providers_service.fetch_market_providers(down)).data == providers
    assert (await providers_service.fetch_market_providers(down)).data == providers
    assert down.calls == 1
