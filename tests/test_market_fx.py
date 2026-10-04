"""UX-08: GET /market/fx sirve el USD/EUR del BCE, cacheado 24 h.

El backend no convierte nada — solo sirve el numero. Lo que se prueba aqui es la
politica de caida: si la fuente falla, se reutiliza el ultimo valor conocido marcado
`stale`, y sin valor previo no se inventa ninguno (el cliente se queda en USD).
"""
import time

import httpx
import pytest

from steam.services.fx import fetch_fx_rate as _fetch_fx_rate
from steam.domain.models import Fetched
from stores import FX_CACHE_TTL, _fx_cache


class _FakeClient:
    """httpx.AsyncClient minimo: devuelve lo programado y cuenta las llamadas."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = 0

    async def get(self, url, **kwargs):
        self.calls += 1
        r = self._responses.pop(0) if self._responses else self._responses
        if isinstance(r, Exception):
            raise r
        return r


def _resp(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "http://fx"))


@pytest.fixture(autouse=True)
def _clean_fx():
    _fx_cache.clear()
    yield
    _fx_cache.clear()


@pytest.mark.asyncio
async def test_sirve_la_tasa_y_la_cachea():
    c = _FakeClient(_resp({"rates": {"EUR": 0.88067}}))
    assert await _fetch_fx_rate(c) == Fetched(0.88067)
    # Segunda llamada dentro del TTL: no vuelve a salir a la red.
    assert await _fetch_fx_rate(c) == Fetched(0.88067)
    assert c.calls == 1


@pytest.mark.asyncio
async def test_tras_el_ttl_refresca():
    c = _FakeClient(_resp({"rates": {"EUR": 0.88}}), _resp({"rates": {"EUR": 0.91}}))
    await _fetch_fx_rate(c)
    _fx_cache["usdeur"] = (0.88, time.monotonic() - FX_CACHE_TTL - 1)
    assert await _fetch_fx_rate(c) == Fetched(0.91)
    assert c.calls == 2


@pytest.mark.asyncio
async def test_fuente_caida_reutiliza_el_ultimo_valor_como_stale():
    c = _FakeClient(_resp({"rates": {"EUR": 0.88}}), httpx.ConnectError("boom"))
    await _fetch_fx_rate(c)
    _fx_cache["usdeur"] = (0.88, time.monotonic() - FX_CACHE_TTL - 1)
    assert await _fetch_fx_rate(c) == Fetched(0.88, "stale", "unavailable")


@pytest.mark.asyncio
async def test_sin_valor_previo_no_inventa_tasa():
    assert await _fetch_fx_rate(_FakeClient(httpx.ConnectError("boom"))) == Fetched(None, "error", "unavailable")
    assert await _fetch_fx_rate(_FakeClient(_resp({}, status=503))) == Fetched(None, "error", "http_503")


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [0.0, 0.4, 2.5, None, "0.88"])
async def test_tasa_implausible_se_descarta(rate):
    """Un USD/EUR de 0 o de 2.5 es un error de la fuente, no mercado: convertir con
    eso corrompe todos los precios de la app en silencio."""
    assert await _fetch_fx_rate(_FakeClient(_resp({"rates": {"EUR": rate}}))) == Fetched(None, "error", "implausible_rate")


def test_endpoint_devuelve_la_forma_que_espera_el_cliente(client, monkeypatch):
    async def _fake(_):
        return Fetched(0.88067)
    monkeypatch.setattr("steam.services.fx.fetch_fx_rate", _fake)
    body = client.get("/market/fx").json()
    assert body == {"base": "USD", "rates": {"EUR": 0.88067}, "stale": False}


def test_endpoint_sin_tasa_no_es_un_error(client, monkeypatch):
    """Sin tasa el cliente se queda en USD; un 500 haria fallar la query y pintaria
    un toast de error por algo que no lo es."""
    async def _fake(_):
        return Fetched(None, "error", "unavailable")
    monkeypatch.setattr("steam.services.fx.fetch_fx_rate", _fake)
    resp = client.get("/market/fx")
    assert resp.status_code == 200
    assert resp.json() == {"base": "USD", "rates": {}, "stale": True}
