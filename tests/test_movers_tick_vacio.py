"""CAL-10: si compute_movers no saca nada (fuentes caídas), el movers-tick NO puede
reemplazar la tabla: un replace-all con cero filas dejaba la Home sin hot/cold
hasta el siguiente tick bueno, y el workflow seguía en verde."""
from unittest.mock import AsyncMock

import pytest

from steam.domain.models import Fetched
from steam.rankings_repo import movers_repo
from steam.routes import market as market_routes
from steam.services import market as market_service


@pytest.fixture
def tick(client, monkeypatch):
    monkeypatch.setattr(market_routes, "CAP_TICK_TOKEN", "secret123")
    replace = AsyncMock()
    monkeypatch.setattr(movers_repo, "replace_snapshot", replace)

    def _run(result):
        monkeypatch.setattr(market_service, "compute_movers", AsyncMock(return_value=Fetched(result)))
        return client.post("/internal/movers-tick", headers={"X-Cap-Token": "secret123"})

    return _run, replace


def test_sin_fuentes_conserva_el_snapshot_anterior(tick):
    run, replace = tick

    resp = run({"hot": [], "cold": []})

    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "count": 0, "kept_previous": True}
    replace.assert_not_awaited()


def test_con_datos_reemplaza_como_siempre(tick):
    run, replace = tick
    item = {"name": "AK-47 | Redline (Field-Tested)", "priceLatest": 20.0}

    resp = run({"hot": [item], "cold": []})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "count": 1}
    replace.assert_awaited_once()
    assert len(replace.await_args.args[0]) == 1
