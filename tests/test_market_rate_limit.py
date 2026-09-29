"""SEC-03: las lecturas de /market/* tienen rate limit propio (60/60 s por IP),
separado del de /auth/* (10/60 s), para que un bucle no agote la cuota de
steamwebapi de todos y para que abrir Market (~6 llamadas) no dispare nada."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import auth.service as auth_service
from main import app
from stores import MARKET_RATE_LIMIT_CALLS, RATE_LIMIT_CALLS, _rate_store

MARKET_READS = [
    "/market/movers", "/market/items", "/market/price", "/market/trending",
    "/market/index", "/market/cap-history", "/market/providers", "/market/prices",
    "/market/fx",
]


def _request(ip: str = "203.0.113.7"):
    return SimpleNamespace(headers={}, client=SimpleNamespace(host=ip))


@pytest.fixture(autouse=True)
def _clean():
    _rate_store.clear()
    yield
    _rate_store.clear()


def test_market_budget_allows_60_then_rejects():
    for _ in range(MARKET_RATE_LIMIT_CALLS):
        auth_service.market_rate_limit(_request())
    with pytest.raises(HTTPException) as exc:
        auth_service.market_rate_limit(_request())
    assert exc.value.status_code == 429


def test_market_and_auth_buckets_do_not_share_a_counter():
    for _ in range(RATE_LIMIT_CALLS):
        auth_service.market_rate_limit(_request())        # 10 lecturas de mercado...
    auth_service._rate_limit("203.0.113.7")                # ...no consumen el cupo de auth
    for _ in range(RATE_LIMIT_CALLS - 1):
        auth_service._rate_limit("203.0.113.7")
    with pytest.raises(HTTPException):
        auth_service._rate_limit("203.0.113.7")            # el de auth sí se agota a los 10


def test_budget_is_per_ip():
    for _ in range(MARKET_RATE_LIMIT_CALLS):
        auth_service.market_rate_limit(_request("198.51.100.1"))
    auth_service.market_rate_limit(_request("198.51.100.2"))   # otra IP, sin 429


@pytest.mark.parametrize("path", MARKET_READS)
def test_every_market_read_declares_the_limiter(path):
    route = next(r for r in app.routes if getattr(r, "path", None) == path and "GET" in r.methods)
    deps = [d.call for d in route.dependant.dependencies]
    assert auth_service.market_rate_limit in deps, f"{path} sin market_rate_limit"


def test_opening_market_does_not_trip_the_limit(client, monkeypatch):
    """Riesgo real del cambio: el flujo normal de abrir Market no puede dar 429.
    Se comprueba en el limiter (6 llamadas distintas) sin tocar upstreams."""
    for _ in range(6):
        auth_service.market_rate_limit(_request())
    assert len(_rate_store["market:203.0.113.7"]) == 6
