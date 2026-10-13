"""COST-02 · Breaker por host (worker/net/breaker.py) y la capa de petición (worker/net/retry.py)."""
import logging

import fakeredis
import httpx
import pybreaker
import pytest
import stamina

from worker.net import retry
from worker.net.breaker import Breakers, RateLimited
from worker.net.gate import Gate, HostCoolingDown
from worker.settings import HostLimit

LIMITS = {"steamcommunity.com": HostLimit(rate=1000, per_s=1, burst=100, jitter=(0, 0), reset_timeout=900, steam=True),
          "api.skinport.com": HostLimit(rate=1000, per_s=1, burst=100, jitter=(0, 0))}


@pytest.fixture(autouse=True)
def _no_waits():
    with stamina.set_testing(True, attempts=3):
        yield


@pytest.fixture
def env():
    server = fakeredis.FakeServer()
    gate = Gate(fakeredis.aioredis.FakeRedis(server=server), LIMITS)
    breakers = Breakers(fakeredis.FakeRedis(server=server), LIMITS)
    return server, gate, breakers


def _client(statuses, headers=None, calls=None):
    it = iter(statuses)

    def handler(req):
        if calls is not None:
            calls.append(req.url.host)
        s = next(it)
        if isinstance(s, Exception):
            raise s
        return httpx.Response(s, headers=headers or {})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _get(env, client, host="api.skinport.com"):
    _, gate, breakers = env
    return await retry.request(client, "GET", f"https://{host}/v1/items", gate=gate, breakers=breakers)


def test_cinco_fallos_abren_y_el_estado_se_comparte(env, caplog):
    server, _, breakers = env
    cb = breakers.for_host("api.skinport.com")
    assert cb.reset_timeout == 300 and breakers.for_host("steamcommunity.com").reset_timeout == 900
    caplog.set_level(logging.WARNING)
    for _ in range(4):
        with pytest.raises(ValueError), cb.calling():
            raise ValueError("caída")
    with pytest.raises(pybreaker.CircuitBreakerError), cb.calling():
        raise ValueError("caída")
    assert cb.current_state == "open"
    # Otro proceso (otra instancia, mismo Valkey) ve el circuito abierto.
    other = Breakers(fakeredis.FakeRedis(server=server), LIMITS).for_host("api.skinport.com")
    assert other.current_state == "open"
    assert "[breaker] host=api.skinport.com closed -> open" in caplog.text


def test_un_429_no_cuenta_como_fallo(env):
    cb = env[2].for_host("api.skinport.com")
    for _ in range(6):
        with pytest.raises(RateLimited), cb.calling():
            raise RateLimited("api.skinport.com", 30)
    assert cb.current_state == "closed" and cb.fail_counter == 0


def test_half_open_cierra_tras_un_exito(env):
    cb = env[2].for_host("api.skinport.com")
    cb.reset_timeout = 0
    for _ in range(5):
        with pytest.raises((ValueError, pybreaker.CircuitBreakerError)), cb.calling():
            raise ValueError
    with cb.calling():
        pass
    assert cb.current_state == "closed"


async def test_5xx_se_reintenta_tres_veces(env):
    calls = []
    with pytest.raises(retry.ServerError):
        await _get(env, _client([502, 503, 500], calls=calls))
    assert len(calls) == 3
    assert (await _get(env, _client([502, 200]))).status_code == 200


async def test_error_de_red_se_reintenta(env):
    r = await _get(env, _client([httpx.ConnectError("x"), 200]))
    assert r.status_code == 200


async def test_429_de_steam_abre_cooldown_sin_reintento(env):
    _, gate, breakers = env
    calls = []
    with pytest.raises(RateLimited):
        await _get(env, _client([429, 200], calls=calls), host="steamcommunity.com")
    assert len(calls) == 1
    assert await gate.cooldown_left("steamcommunity.com") > 3500
    with pytest.raises(HostCoolingDown):
        await _get(env, _client([200]), host="steamcommunity.com")
    assert breakers.for_host("steamcommunity.com").fail_counter == 0


async def test_429_sin_retry_after_se_reintenta_fuera_de_steam(env):
    assert (await _get(env, _client([429, 200]))).status_code == 200


async def test_429_con_retry_after_se_devuelve_al_job(env):
    calls = []
    with pytest.raises(RateLimited) as e:
        await _get(env, _client([429, 200], headers={"Retry-After": "42"}, calls=calls))
    assert e.value.retry_after == 42.0 and len(calls) == 1


async def test_breaker_abierto_corta_sin_llamar(env):
    cb = env[2].for_host("api.skinport.com")
    cb.open()
    calls = []
    with pytest.raises(pybreaker.CircuitBreakerError):
        await _get(env, _client([200], calls=calls))
    assert calls == []


def test_parse_retry_after():
    assert retry.parse_retry_after("5") == 5.0
    assert retry.parse_retry_after(None) is None
    assert retry.parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
