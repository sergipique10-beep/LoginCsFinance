"""SEC-09: /me/stats* proxifica Leetify con la clave en el servidor.

Lo que se fija aquí: la clave nunca sale en la URL ni se acepta de fuera, el SteamID
sale solo del JWT (no hay forma de pedir las estadísticas de otro), y sin token no
se llega a Leetify.
"""
from unittest.mock import AsyncMock

import httpx
import pytest

from auth.service import leetify_rate_limit, require_jwt
from main import app
from stats import router as stats_router
from stores import STATS_RATE_LIMIT_CALLS, _leetify_cache, _rate_store
from tests.conftest import STEAM_ID

PROFILE = {"steam64_id": STEAM_ID, "winrate": 0.56, "total_matches": 12}
KEY = "clave-de-prueba"


@pytest.fixture(autouse=True)
def _leetify(monkeypatch):
    _leetify_cache.clear()
    monkeypatch.setattr(stats_router, "LEETIFY_API_KEY", KEY)
    yield
    _leetify_cache.clear()


def _upstream(monkeypatch, response=None, exc=None):
    mock = AsyncMock(return_value=response, side_effect=exc)
    monkeypatch.setattr(app.state.http_client, "get", mock)
    return mock


def _ok(body=PROFILE):
    return httpx.Response(200, json=body)


def test_sin_token_no_hay_acceso_ni_llamada_a_leetify(client, monkeypatch):
    mock = _upstream(monkeypatch, _ok())
    app.dependency_overrides.pop(require_jwt)

    for path in ("/me/stats", "/me/stats/matches"):
        assert client.get(path).status_code in (401, 403)
    mock.assert_not_awaited()


def test_usa_el_steam_id_del_jwt_y_la_clave_va_en_cabecera(client, monkeypatch):
    mock = _upstream(monkeypatch, _ok())

    resp = client.get("/me/stats")

    assert resp.status_code == 200
    assert resp.json() == PROFILE
    url = mock.await_args.args[0]
    assert url.endswith("/v3/profile")
    assert mock.await_args.kwargs["params"] == {"steam64_id": STEAM_ID}
    assert mock.await_args.kwargs["headers"] == {"Authorization": f"Bearer {KEY}"}
    assert KEY not in url


def test_ignora_un_steam64_id_del_cliente(client, monkeypatch):
    mock = _upstream(monkeypatch, _ok())

    client.get("/me/stats", params={"steam64_id": "76561198000000001"})

    assert mock.await_args.kwargs["params"] == {"steam64_id": STEAM_ID}


def test_matches_pasa_la_lista_tal_cual(client, monkeypatch):
    mock = _upstream(monkeypatch, _ok([{"id": "m1"}]))

    resp = client.get("/me/stats/matches")

    assert resp.json() == [{"id": "m1"}]
    assert mock.await_args.args[0].endswith("/v3/profile/matches")


def test_cachea_por_usuario_y_ruta(client, monkeypatch):
    mock = _upstream(monkeypatch, _ok())

    client.get("/me/stats")
    client.get("/me/stats")
    assert mock.await_count == 1

    client.get("/me/stats/matches")          # otra ruta: llamada nueva
    assert mock.await_count == 2


@pytest.mark.parametrize("upstream,expected", [
    (httpx.Response(404), 404),   # usuario sin perfil de Leetify: caso normal
    (httpx.Response(401), 502),   # clave rota: fallo nuestro, no del usuario
    (httpx.Response(429), 502),
    (httpx.Response(500), 502),
])
def test_traduce_errores_de_leetify_y_no_los_cachea(client, monkeypatch, upstream, expected):
    mock = _upstream(monkeypatch, upstream)

    assert client.get("/me/stats").status_code == expected
    assert client.get("/me/stats").status_code == expected
    assert mock.await_count == 2


def test_timeout_es_504_y_red_caida_es_502(client, monkeypatch):
    _upstream(monkeypatch, exc=httpx.ReadTimeout("t"))
    assert client.get("/me/stats").status_code == 504
    _upstream(monkeypatch, exc=httpx.ConnectError("c"))
    assert client.get("/me/stats").status_code == 502


def test_sin_clave_configurada_es_503_y_no_se_llama(client, monkeypatch):
    mock = _upstream(monkeypatch, _ok())
    monkeypatch.setattr(stats_router, "LEETIFY_API_KEY", "")

    assert client.get("/me/stats").status_code == 503
    mock.assert_not_awaited()


def test_rate_limit_propio_por_ip(client, monkeypatch):
    _upstream(monkeypatch, _ok())
    _rate_store.clear()

    for _ in range(STATS_RATE_LIMIT_CALLS):
        assert client.get("/me/stats").status_code == 200
    assert client.get("/me/stats").status_code == 429
    _rate_store.clear()


@pytest.mark.parametrize("path", ["/me/stats", "/me/stats/matches"])
def test_ambas_rutas_declaran_el_limiter(path):
    route = next(r for r in app.routes if getattr(r, "path", None) == path)
    assert leetify_rate_limit in [d.call for d in route.dependant.dependencies]
