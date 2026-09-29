"""UX-16: el histórico de la cartera se persiste en servidor por SteamID.

Hasta el 2026-09-29 vivía solo en el localStorage del móvil y `wipeLocalAccountData()`
lo borraba en cada logout. Lo que se fija aquí es el contrato que impide el fallo más
grave de mover ese dato al servidor: que un usuario escriba o lea la serie de otro.
"""
from unittest.mock import AsyncMock

import pytest

from portfolio import router as portfolio_router
from tests.conftest import STEAM_ID


# ── GET /portfolio/history ────────────────────────────────────────────────────

def test_history_usa_el_dueno_del_jwt(client, monkeypatch):
    mock = AsyncMock(return_value=[{"provider": "buff", "date": "2026-09-29", "value": 5537.36}])
    monkeypatch.setattr(portfolio_router.repo, "list_for_user", mock)

    resp = client.get("/portfolio/history", params={"provider": "buff"})

    assert resp.status_code == 200
    assert resp.json() == {"history": [{"provider": "buff", "date": "2026-09-29", "value": 5537.36}]}
    mock.assert_awaited_once_with(STEAM_ID, "buff")


def test_history_exige_provider(client):
    # Sin provider no hay serie que devolver: son series distintas por mercado.
    assert client.get("/portfolio/history").status_code == 422


@pytest.mark.parametrize("provider", ["", "steamm", "mercado-inventado", "BUFF; drop table"])
def test_history_rechaza_provider_desconocido(client, provider):
    resp = client.get("/portfolio/history", params={"provider": provider})
    assert resp.status_code == 400


def test_history_normaliza_mayusculas(client, monkeypatch):
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr(portfolio_router.repo, "list_for_user", mock)

    assert client.get("/portfolio/history", params={"provider": "BUFF"}).status_code == 200
    mock.assert_awaited_once_with(STEAM_ID, "buff")


# ── POST /portfolio/snapshot ──────────────────────────────────────────────────

def test_snapshot_usa_el_jwt_e_ignora_el_steam_id_del_cuerpo(client, monkeypatch):
    """El test que más importa de UX-16.

    Si el `steam_id` se tomara del cuerpo, cualquiera con una sesión válida podría
    sobrescribir el histórico de cartera de otro usuario.
    """
    mock = AsyncMock(return_value={"provider": "buff", "date": "2026-09-29", "value": 100.0})
    monkeypatch.setattr(portfolio_router.repo, "upsert_today", mock)

    resp = client.post(
        "/portfolio/snapshot",
        json={"provider": "buff", "value": 100.0, "steam_id": "76561190000000000"},
    )

    assert resp.status_code == 201
    mock.assert_awaited_once_with(STEAM_ID, "buff", 100.0)


def test_snapshot_rechaza_provider_desconocido(client):
    resp = client.post("/portfolio/snapshot", json={"provider": "kraken", "value": 10})
    assert resp.status_code == 400


@pytest.mark.parametrize("value", [-1, -0.01, 10**9])
def test_snapshot_rechaza_valores_fuera_de_rango(client, value):
    # Negativo no es una cartera; 1e9 es un desbordamiento o un cliente roto.
    resp = client.post("/portfolio/snapshot", json={"provider": "buff", "value": value})
    assert resp.status_code == 422


def test_snapshot_acepta_cero(client, monkeypatch):
    # Una cartera vacía es un dato legítimo: el usuario vendió todo.
    mock = AsyncMock(return_value={"provider": "buff", "date": "2026-09-29", "value": 0.0})
    monkeypatch.setattr(portfolio_router.repo, "upsert_today", mock)

    assert client.post("/portfolio/snapshot", json={"provider": "buff", "value": 0}).status_code == 201


def test_snapshot_no_acepta_fecha_del_cliente(client, monkeypatch):
    """La fecha la pone el servidor (repo.upsert_today): un reloj mal puesto en el
    dispositivo no debe sembrar la serie con fechas futuras."""
    mock = AsyncMock(return_value={})
    monkeypatch.setattr(portfolio_router.repo, "upsert_today", mock)

    client.post("/portfolio/snapshot", json={"provider": "buff", "value": 50, "date": "2099-01-01"})

    # La firma solo lleva (steam_id, provider, value): no hay por dónde colar la fecha.
    mock.assert_awaited_once_with(STEAM_ID, "buff", 50.0)
