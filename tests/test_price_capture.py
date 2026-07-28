from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from steam import price_capture


def test_canonical_price_prefers_latestsell():
    assert price_capture._canonical_price(
        {"pricelatestsell": 43.15, "pricelatest": 41.35, "pricemedian": 42.71}
    ) == 43.15


def test_canonical_price_falls_back_when_zero():
    assert price_capture._canonical_price(
        {"pricelatestsell": 0, "pricelatest": 0, "pricemedian": 42.71}
    ) == 42.71


def test_canonical_price_none_when_all_missing():
    assert price_capture._canonical_price({}) is None


@pytest.mark.asyncio
async def test_seed_tracked_only_when_empty(monkeypatch):
    monkeypatch.setattr(price_capture.repo, "count_tracked", AsyncMock(return_value=0))
    reg = AsyncMock()
    monkeypatch.setattr(price_capture.repo, "register_tracked", reg)

    n = await price_capture.seed_tracked()

    assert n > 0
    reg.assert_awaited_once()
    assert reg.await_args.args[1] == "top_n"


@pytest.mark.asyncio
async def test_seed_tracked_skips_when_populated(monkeypatch):
    monkeypatch.setattr(price_capture.repo, "count_tracked", AsyncMock(return_value=5))
    reg = AsyncMock()
    monkeypatch.setattr(price_capture.repo, "register_tracked", reg)

    n = await price_capture.seed_tracked()

    assert n == 0
    reg.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_snapshots_and_marks(monkeypatch):
    monkeypatch.setattr(price_capture, "PRICE_LOOKUP_CAP", 400)
    monkeypatch.setattr(price_capture.repo, "fetch_tracked",
                        AsyncMock(return_value=["AK-47 | Redline (Field-Tested)"]))
    upsert = AsyncMock()
    mark = AsyncMock()
    monkeypatch.setattr(price_capture.repo, "upsert_prices", upsert)
    monkeypatch.setattr(price_capture.repo, "mark_captured", mark)
    # el lookup por-nombre devuelve un item con precio y volumen
    monkeypatch.setattr(price_capture, "_lookup_item",
                        AsyncMock(return_value={"pricelatestsell": 43.15, "sold24h": 69}))

    out = await price_capture.capture(MagicMock())

    assert out["captured"] == 1
    row = upsert.await_args.args[0][0]
    assert row["market_hash_name"] == "AK-47 | Redline (Field-Tested)"
    assert row["price"] == 43.15
    assert row["volume"] == 69
    mark.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_skips_item_without_price(monkeypatch):
    monkeypatch.setattr(price_capture, "PRICE_LOOKUP_CAP", 400)
    monkeypatch.setattr(price_capture.repo, "fetch_tracked",
                        AsyncMock(return_value=["Bad | Skin (Field-Tested)"]))
    upsert = AsyncMock()
    monkeypatch.setattr(price_capture.repo, "upsert_prices", upsert)
    monkeypatch.setattr(price_capture.repo, "mark_captured", AsyncMock())
    monkeypatch.setattr(price_capture, "_lookup_item",
                        AsyncMock(return_value={"pricelatestsell": 0}))

    out = await price_capture.capture(MagicMock())

    assert out["captured"] == 0
    assert out["skipped"] == 1
    upsert.assert_awaited_once()
    assert upsert.await_args.args[0] == []  # nada que upsertear


class TestTroceado:
    """Cada corrida es UN LOTE; el workflow repite hasta `pendientes` == 0.

    Render free corta las peticiones largas: 200 skins (~11 min) pasaba, 400
    (~22 min) daba 502 y se perdía la corrida entera. De ahí el troceado.

    El invariante que lo sostiene: `pendientes` tiene que bajar en cada lote,
    incluso cuando los lookups fallan. Si no, el bucle del workflow gira
    reintentando las mismas skins hasta agotar MAX_LOTES.
    """

    @staticmethod
    def _preparar(monkeypatch, pendientes, lote, *, lookup=None):
        monkeypatch.setattr(price_capture, "PRICE_LOOKUP_CAP", lote)
        monkeypatch.setattr(price_capture.repo, "count_pending",
                            AsyncMock(return_value=pendientes))
        n = min(pendientes, lote)
        fetch = AsyncMock(return_value=[f"Skin{i}" for i in range(n)])
        monkeypatch.setattr(price_capture.repo, "fetch_tracked", fetch)
        mark = AsyncMock()
        monkeypatch.setattr(price_capture.repo, "upsert_prices", AsyncMock())
        monkeypatch.setattr(price_capture.repo, "mark_captured", mark)
        monkeypatch.setattr(
            price_capture, "_lookup_item",
            lookup or AsyncMock(return_value={"pricelatestsell": 1.0}),
        )
        return fetch, mark

    @pytest.mark.asyncio
    async def test_pide_un_lote_y_reporta_lo_que_queda(self, monkeypatch):
        fetch, _ = self._preparar(monkeypatch, pendientes=391, lote=150)

        out = await price_capture.capture(MagicMock())

        assert fetch.await_args.args[0] == 150
        assert out["captured"] == 150
        assert out["pendientes"] == 241      # el workflow vuelve a llamar

    @pytest.mark.asyncio
    async def test_el_ultimo_lote_cierra_el_bucle(self, monkeypatch):
        self._preparar(monkeypatch, pendientes=91, lote=150)

        out = await price_capture.capture(MagicMock())

        assert out["pendientes"] == 0        # el workflow para

    @pytest.mark.asyncio
    async def test_excluye_las_ya_capturadas_hoy(self, monkeypatch):
        """Sin el filtro por fecha, cada lote repetiría el mismo conjunto."""
        fetch, _ = self._preparar(monkeypatch, pendientes=200, lote=150)

        await price_capture.capture(MagicMock())

        assert fetch.await_args.kwargs["before"] == date.today().isoformat()

    @pytest.mark.asyncio
    async def test_las_que_fallan_tambien_avanzan_la_rueda(self, monkeypatch):
        """Si no, una skin rota deja `pendientes` clavado y el bucle no termina."""
        _, mark = self._preparar(
            monkeypatch, pendientes=10, lote=150,
            lookup=AsyncMock(side_effect=RuntimeError("404")),
        )

        out = await price_capture.capture(MagicMock())

        assert out["captured"] == 0
        assert out["errors"] == 10
        assert out["pendientes"] == 0                    # no se reintentan
        assert len(mark.await_args.args[0]) == 10        # marcadas igualmente

    @pytest.mark.asyncio
    async def test_sin_pendientes_no_hace_lookups(self, monkeypatch):
        self._preparar(monkeypatch, pendientes=0, lote=150)

        out = await price_capture.capture(MagicMock())

        assert out["tracked_run"] == 0
        assert out["pendientes"] == 0


@pytest.mark.asyncio
async def test_capture_counts_errors(monkeypatch):
    monkeypatch.setattr(price_capture, "PRICE_LOOKUP_CAP", 400)
    monkeypatch.setattr(price_capture.repo, "fetch_tracked",
                        AsyncMock(return_value=["X | Y (Field-Tested)"]))
    monkeypatch.setattr(price_capture.repo, "upsert_prices", AsyncMock())
    monkeypatch.setattr(price_capture.repo, "mark_captured", AsyncMock())
    monkeypatch.setattr(price_capture, "_lookup_item",
                        AsyncMock(side_effect=RuntimeError("boom")))

    out = await price_capture.capture(MagicMock())

    assert out["errors"] == 1
    assert out["captured"] == 0
