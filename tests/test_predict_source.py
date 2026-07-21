"""Tests de la selección de fuente histórica en predict/service.py.

La serie propia (precios_historicos) tiene prioridad sobre CSFloat; si aún no
hay suficientes puntos acumulados, o Supabase falla, se cae a CSFloat.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from predict import service
from predict.service import _MIN_PUNTOS_PROPIOS


def _pts(n, precio=10.0):
    return [{"date": f"2026-01-{i+1:02d}", "price": precio, "volume": 5} for i in range(n)]


def test_usa_serie_propia_si_hay_suficientes_puntos(monkeypatch):
    propios = _pts(_MIN_PUNTOS_PROPIOS, precio=42.0)
    repo = MagicMock(fetch_prices=AsyncMock(return_value=propios))
    monkeypatch.setattr("steam.price_history_repo.fetch_prices", repo.fetch_prices)
    csfloat = AsyncMock(return_value=_pts(50, precio=99.0))
    monkeypatch.setattr("steam.services._fetch_history_for_item", csfloat)

    out = asyncio.run(service._historico(MagicMock(), "AK"))

    assert out[0]["price"] == 42.0     # vino de la tabla propia
    csfloat.assert_not_awaited()        # no se gastó cuota de steamwebapi


def test_cae_a_csfloat_si_pocos_puntos_propios(monkeypatch):
    repo = MagicMock(fetch_prices=AsyncMock(return_value=_pts(_MIN_PUNTOS_PROPIOS - 1)))
    monkeypatch.setattr("steam.price_history_repo.fetch_prices", repo.fetch_prices)
    csfloat = AsyncMock(return_value=_pts(50, precio=99.0))
    monkeypatch.setattr("steam.services._fetch_history_for_item", csfloat)

    out = asyncio.run(service._historico(MagicMock(), "AK"))

    assert out[0]["price"] == 99.0
    csfloat.assert_awaited_once()


def test_cae_a_csfloat_si_supabase_falla(monkeypatch):
    repo = MagicMock(fetch_prices=AsyncMock(side_effect=RuntimeError("sin credenciales")))
    monkeypatch.setattr("steam.price_history_repo.fetch_prices", repo.fetch_prices)
    csfloat = AsyncMock(return_value=_pts(30, precio=77.0))
    monkeypatch.setattr("steam.services._fetch_history_for_item", csfloat)

    out = asyncio.run(service._historico(MagicMock(), "AK"))

    assert out[0]["price"] == 77.0      # el fallo de Supabase no rompe la predicción
    csfloat.assert_awaited_once()
