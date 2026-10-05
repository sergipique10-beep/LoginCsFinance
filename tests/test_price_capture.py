from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from steam.adapters.steam_adapter import adapt_item

from steam.services import price_capture_service as price_capture
from steam.domain.rules import canonical_price
from steam.api import steam_client


@pytest.fixture(autouse=True)
def _sin_supabase(monkeypatch):
    """Ningún test de este fichero toca Supabase de verdad.

    Antes de PERF-11 varios tests mockeaban fetch_tracked pero no count_pending,
    que salía contra producción (y fallaba sin .env). Presupuesto holgado por
    defecto: los tests que no son de presupuesto no deben notarlo.
    """
    monkeypatch.setattr(price_capture, "PRICE_DAILY_BUDGET", 10_000)
    monkeypatch.setattr(price_capture.repo, "count_captured_on", AsyncMock(return_value=0))
    monkeypatch.setattr(price_capture.repo, "count_pending", AsyncMock(return_value=10))


def test_canonical_price_prefers_latestsell():
    assert canonical_price(adapt_item(
        {"pricelatestsell": 43.15, "pricelatest": 41.35, "pricemedian": 42.71})
    ) == 43.15


def test_canonical_price_falls_back_when_zero():
    assert canonical_price(adapt_item(
        {"pricelatestsell": 0, "pricelatest": 0, "pricemedian": 42.71})
    ) == 42.71


def test_canonical_price_none_when_all_missing():
    assert canonical_price(adapt_item({})) is None and canonical_price(None) is None


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
    monkeypatch.setattr(price_capture, "lookup_item",
                        AsyncMock(return_value=adapt_item({"pricelatestsell": 43.15, "sold24h": 69})))

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
    monkeypatch.setattr(price_capture, "lookup_item",
                        AsyncMock(return_value=adapt_item({"pricelatestsell": 0})))

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
            price_capture, "lookup_item",
            lookup or AsyncMock(return_value=adapt_item({"pricelatestsell": 1.0})),
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
            lookup=AsyncMock(side_effect=UpstreamError(404, "not found")),   # fallo de la fuente, tipado
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
    monkeypatch.setattr(price_capture, "lookup_item",
                        AsyncMock(side_effect=SourceUnavailable("boom")))

    out = await price_capture.capture(MagicMock())

    assert out["errors"] == 1
    assert out["captured"] == 0


# ── PERF-09: 402 = cuota agotada → abortar el lote ───────────────────────────

import httpx

from steam.errors import QuotaExhausted, SourceUnavailable, UpstreamError


@pytest.mark.asyncio
async def test_lookup_raises_quota_exhausted_on_402(monkeypatch):
    monkeypatch.setattr(steam_client._history_limiter, "acquire", AsyncMock())
    client = MagicMock()
    client.get = AsyncMock(return_value=httpx.Response(402, text='{"status":402,"message":"monthly limit"}'))

    with pytest.raises(QuotaExhausted):
        await price_capture.lookup_item(client, "AK")


@pytest.mark.asyncio
async def test_capture_aborts_batch_on_quota_exhausted(monkeypatch):
    monkeypatch.setattr(price_capture, "PRICE_LOOKUP_CAP", 400)
    monkeypatch.setattr(price_capture.repo, "count_pending", AsyncMock(return_value=3))
    monkeypatch.setattr(price_capture.repo, "fetch_tracked", AsyncMock(return_value=["A", "B", "C"]))
    upsert, mark = AsyncMock(), AsyncMock()
    monkeypatch.setattr(price_capture.repo, "upsert_prices", upsert)
    monkeypatch.setattr(price_capture.repo, "mark_captured", mark)
    lookup = AsyncMock(side_effect=[adapt_item({"pricelatest": 5}), QuotaExhausted("402"), adapt_item({"pricelatest": 7})])
    monkeypatch.setattr(price_capture, "lookup_item", lookup)

    out = await price_capture.capture(MagicMock())

    assert lookup.await_count == 2                     # C no se intenta
    assert out["quota_exhausted"] is True
    assert out["captured"] == 1 and out["errors"] == 1 and out["pendientes"] == 1
    mark.assert_awaited_once()
    assert mark.await_args.args[0] == ["A", "B"]        # B intentada: la rueda avanza


class TestPresupuestoDiario:
    """PERF-11: el price-tick gasta como mucho PRICE_DAILY_BUDGET lookups al día.

    Sin tope gastaba ~800/día (una por skin seguida) frente a ~333/día de todo
    el plan Starter, y agotaba la cuota mensual hacia el día 13 del ciclo.
    El gasto del día se deduce de la BD (skins con last_captured = hoy), así
    que el tope se respeta aunque el workflow haga varios lotes.
    """

    @staticmethod
    def _preparar(monkeypatch, *, presupuesto, usado, pendientes, lote=150):
        monkeypatch.setattr(price_capture, "PRICE_DAILY_BUDGET", presupuesto)
        monkeypatch.setattr(price_capture, "PRICE_LOOKUP_CAP", lote)
        monkeypatch.setattr(price_capture.repo, "count_captured_on",
                            AsyncMock(return_value=usado))
        monkeypatch.setattr(price_capture.repo, "count_pending",
                            AsyncMock(return_value=pendientes))
        fetch = AsyncMock(side_effect=lambda n, before: [f"S{i}" for i in range(min(n, pendientes))])
        monkeypatch.setattr(price_capture.repo, "fetch_tracked", fetch)
        monkeypatch.setattr(price_capture.repo, "upsert_prices", AsyncMock())
        monkeypatch.setattr(price_capture.repo, "mark_captured", AsyncMock())
        lookup = AsyncMock(return_value=adapt_item({"pricelatestsell": 1.0}))
        monkeypatch.setattr(price_capture, "lookup_item", lookup)
        return fetch, lookup

    @pytest.mark.asyncio
    async def test_el_lote_no_pasa_del_presupuesto_restante(self, monkeypatch):
        fetch, lookup = self._preparar(monkeypatch, presupuesto=250, usado=200, pendientes=600)

        out = await price_capture.capture(MagicMock())

        assert fetch.await_args.args[0] == 50
        assert lookup.await_count == 50
        assert out["pendientes"] == 0                    # el workflow para
        assert out["fuera_de_presupuesto"] == 550        # y lo dice
        assert out["presupuesto_restante"] == 0

    @pytest.mark.asyncio
    async def test_presupuesto_agotado_no_hace_lookups(self, monkeypatch):
        fetch, lookup = self._preparar(monkeypatch, presupuesto=250, usado=250, pendientes=400)

        out = await price_capture.capture(MagicMock())

        fetch.assert_not_awaited()
        lookup.assert_not_awaited()
        assert out["tracked_run"] == 0
        assert out["pendientes"] == 0
        assert out["fuera_de_presupuesto"] == 400

    @pytest.mark.asyncio
    async def test_pendientes_cuenta_solo_lo_que_cabe(self, monkeypatch):
        """Lote 1 de 2: quedan 100 dentro del presupuesto, no las 600."""
        self._preparar(monkeypatch, presupuesto=250, usado=0, pendientes=600)

        out = await price_capture.capture(MagicMock())

        assert out["tracked_run"] == 150
        assert out["pendientes"] == 100
        assert out["fuera_de_presupuesto"] == 350
        assert out["presupuesto_restante"] == 100

    @pytest.mark.asyncio
    async def test_con_presupuesto_holgado_cubre_todo(self, monkeypatch):
        """Ampliar el plan = subir la variable; la población entera cabe."""
        self._preparar(monkeypatch, presupuesto=5000, usado=0, pendientes=120)

        out = await price_capture.capture(MagicMock())

        assert out["tracked_run"] == 120
        assert out["pendientes"] == 0
        assert out["fuera_de_presupuesto"] == 0


def test_presupuesto_sale_de_la_variable_de_entorno(monkeypatch):
    """Escalar el plan de steamwebapi no debe exigir tocar código."""
    import importlib
    import settings

    monkeypatch.setenv("PRICE_DAILY_BUDGET", "1234")
    try:
        assert importlib.reload(settings).PRICE_DAILY_BUDGET == 1234
    finally:
        monkeypatch.delenv("PRICE_DAILY_BUDGET")
        importlib.reload(settings)
