"""El trending-tick registra el top N del ranking en tracked_skins.

Sin esto los items del ranking no tienen serie propia en precios_historicos y
toda predicción sobre ellos cae a CSFloat (predict/service.py). Medido antes del
cambio: 17 de 18 items del trending no existían en tracked_skins.

El registro es best-effort a propósito: la captura del ranking es lo que sirve
la pantalla, y no puede caerse porque falle una escritura secundaria.
"""
from unittest.mock import AsyncMock

import pytest

from steam.routes import market as market_routes


def _item(nombre: str) -> dict:
    return {"name": nombre, "weaponType": "Rifle", "priceLatest": 10.0}


@pytest.fixture
def tick(client, monkeypatch):
    """Prepara el trending-tick con todo mockeado salvo lo que se está probando."""
    monkeypatch.setattr(market_routes, "CAP_TICK_TOKEN", "secret123")
    monkeypatch.setattr(market_routes.trending_repo, "upsert_rows", AsyncMock())
    monkeypatch.setattr(market_routes.trending_repo, "purge_stale", AsyncMock(return_value=0))

    reg = AsyncMock()
    monkeypatch.setattr("steam.price_history_repo.register_tracked", reg)

    def _run(items):
        monkeypatch.setattr(market_routes, "_compute_trending", AsyncMock(return_value=items))
        return client.post("/internal/trending-tick", headers={"X-Cap-Token": "secret123"})

    return _run, reg


def test_registra_el_top_n_por_turnover(tick, monkeypatch):
    """`items` ya viene ordenado por turnover desde _diversificar → basta el slice."""
    run, reg = tick
    monkeypatch.setattr(market_routes, "TRENDING_TRACK_TOP", 3)

    resp = run([_item(f"Skin{i}") for i in range(10)])

    assert resp.status_code == 200
    assert resp.json()["tracked"] == 3
    nombres, source = reg.await_args.args
    assert nombres == ["Skin0", "Skin1", "Skin2"]
    assert source == "trending"


def test_no_registra_mas_de_los_que_hay(tick, monkeypatch):
    run, reg = tick
    monkeypatch.setattr(market_routes, "TRENDING_TRACK_TOP", 80)

    resp = run([_item("Skin0"), _item("Skin1")])

    assert resp.json()["tracked"] == 2
    assert reg.await_args.args[0] == ["Skin0", "Skin1"]


def test_ranking_vacio_no_llama_al_repo(tick):
    run, reg = tick

    resp = run([])

    assert resp.json()["tracked"] == 0
    reg.assert_not_awaited()


def test_un_fallo_al_registrar_no_tumba_la_captura(client, monkeypatch):
    """La captura del ranking es lo que sirve la pantalla: tiene prioridad."""
    monkeypatch.setattr(market_routes, "CAP_TICK_TOKEN", "secret123")
    monkeypatch.setattr(market_routes, "_compute_trending",
                        AsyncMock(return_value=[_item("Skin0")]))
    monkeypatch.setattr(market_routes.trending_repo, "upsert_rows", AsyncMock())
    monkeypatch.setattr(market_routes.trending_repo, "purge_stale", AsyncMock(return_value=0))
    monkeypatch.setattr("steam.price_history_repo.register_tracked",
                        AsyncMock(side_effect=RuntimeError("supabase caída")))

    resp = client.post("/internal/trending-tick", headers={"X-Cap-Token": "secret123"})

    assert resp.status_code == 200
    assert resp.json()["count"] == 1      # el ranking se guardó
    assert resp.json()["tracked"] == 0    # el registro no
