"""SEC-16: /item/history respeta el límite por minuto de steamwebapi (20/60 s).

Visto en el A55 el 2026-10-04 por CDP: abrir ~7 detalles en pocos segundos daba
`502 Steam returned 429` → «El servidor no responde». La ruta iba a steamwebapi sin
pasar por `_history_limiter`. Ahora: limiter con espera corta; ventana llena o 429
de steamwebapi → caché caducada si la hay; si no, 503 `upstream_rate_limit`."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
from steam.routes import items as items_routes
from stores import _item_history_cache

NAME = "AK-47 | Nightwish (Well-Worn)"
URL = f"/item/history?name={NAME}&market=buff&days=30"
KEY = f"{NAME}:10:buff:30"
STALE = -1e9


class _Limiter:
    def __init__(self, busy=False):
        self.busy, self.calls = busy, 0

    async def acquire(self):
        self.calls += 1
        if self.busy:
            await asyncio.sleep(3600)


@pytest.fixture
def upstream(client, monkeypatch):
    """steamwebapi falso: `upstream.status` decide su respuesta."""
    http = MagicMock()
    http.aclose = AsyncMock()
    resp = MagicMock(status_code=200, headers={})
    resp.json.return_value = [{"createdat": "2026-10-01T00:00:00", "price": 10, "quantity": 3}]
    http.get = AsyncMock(return_value=resp)
    monkeypatch.setattr(main.app.state, "http_client", http)
    monkeypatch.setattr(items_routes, "ITEM_HISTORY_LIMITER_TIMEOUT", 0.05)
    limiter = _Limiter()
    monkeypatch.setattr(items_routes, "_history_limiter", limiter)
    _item_history_cache.clear()
    yield http, resp, limiter
    _item_history_cache.clear()


def test_upstream_call_goes_through_the_limiter(client, upstream):
    http, _, limiter = upstream
    assert client.get(URL).status_code == 200
    assert limiter.calls == 1 and http.get.await_count == 1


def test_upstream_429_without_cache_is_503_upstream_rate_limit(client, upstream):
    _, resp, _ = upstream
    resp.status_code = 429
    r = client.get(URL)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "upstream_rate_limit"
    assert r.headers["Retry-After"] == "60"


def test_upstream_429_serves_stale_cache(client, upstream):
    _, resp, _ = upstream
    resp.status_code = 429
    _item_history_cache[KEY] = ([{"date": "2026-09-01", "price": 9.0, "volume": 1}], STALE)
    r = client.get(URL)
    assert r.status_code == 200 and r.json()[0]["price"] == 9.0


def test_full_window_does_not_wait_nor_call_upstream(client, upstream, monkeypatch):
    http, _, _ = upstream
    monkeypatch.setattr(items_routes, "_history_limiter", _Limiter(busy=True))
    r = client.get(URL)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "upstream_rate_limit"
    http.get.assert_not_awaited()
