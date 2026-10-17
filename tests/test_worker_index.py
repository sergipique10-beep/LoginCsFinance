"""COST-05 · Índice de mercado encadenado (worker/pricing/index.py)."""
import pytest

from worker.pricing import index as ix


def stats(n, vol30=10, avg30=10.0, vol24=1, m24=10.0):
    return {f"n{i}": ix.SalesStats(volume_24h=vol24, volume_30d=vol30 * (i + 1), avg_30d=avg30, median_24h=m24)
            for i in range(n)}


def prices(n, p=10.0):
    return {f"n{i}": p for i in range(n)}


def test_cesta_top_por_facturacion_30d():
    st = stats(5) | {"muerto": ix.SalesStats(volume_30d=0, avg_30d=1000.0)}
    b = ix.basket(st, size=3)
    assert list(b) == ["n4", "n3", "n2"] and b["n4"] == 50       # peso = volume_30d
    assert "muerto" not in ix.basket(st)


def test_primer_tick_es_i0_y_precios_constantes_no_mueven():
    st, used = ix.step(ix.IndexState(), "2026-10", stats(150), prices(150), prices(150), 2859545.83, 3545814.37)
    assert (st.priceindex, st.realpriceindex, used) == (2859545.83, 3545814.37, 0)
    assert st.month == "2026-10" and len(st.weights) == 150 and len(st.prev_price) == 150
    st2, used = ix.step(st, "2026-10", stats(150), prices(150), prices(150), 0, None)
    assert st2.priceindex == pytest.approx(2859545.83) and used == 150
    assert st2.realpriceindex == pytest.approx(3545814.37)


def test_subida_uniforme_del_10_por_ciento():
    st, _ = ix.step(ix.IndexState(), "2026-10", stats(150), prices(150), prices(150), 100.0, 100.0)
    st, _ = ix.step(st, "2026-10", stats(150), prices(150, 11.0), prices(150, 11.0), 0, None)
    assert st.priceindex == pytest.approx(110.0) and st.realpriceindex == pytest.approx(110.0)


def test_rebalanceo_de_mes_sin_salto():
    st, _ = ix.step(ix.IndexState(), "2026-10", stats(150), prices(150), prices(150), 100.0, None)
    nuevos = stats(150, vol30=3)                            # otra cesta, otros pesos
    st2, used = ix.step(st, "2026-11", nuevos, prices(150), prices(150), 0, None)
    assert st2.priceindex == pytest.approx(100.0) and used == 150 and st2.weights["n0"] == 3


def test_pocos_nombres_comunes_conserva_el_indice():
    st, _ = ix.step(ix.IndexState(), "2026-10", stats(150), prices(150), prices(150), 100.0, None)
    st2, used = ix.step(st, "2026-10", stats(150), prices(20, 50.0), prices(150), 0, None)
    assert st2.priceindex == 100.0 and used == 0


def test_nombre_que_falta_un_tick_no_pesa():
    st, _ = ix.step(ix.IndexState(), "2026-10", stats(150), prices(150), prices(150), 100.0, None)
    cur = prices(150, 12.0)
    del cur["n149"]                                         # el de más peso desaparece este tick
    st2, used = ix.step(st, "2026-10", stats(150), cur, prices(150), 0, None)
    assert st2.priceindex == pytest.approx(120.0) and used == 149


def test_turnover_24h_steam_sustituye_a_skinport():
    st = {"a": ix.SalesStats(volume_24h=10, median_24h=2.0), "b": ix.SalesStats(volume_24h=5, median_24h=4.0)}
    assert ix.turnover_24h(st, {}) == (40.0, 15)
    assert ix.turnover_24h(st, {"b": (3.0, 6)}) == (38.0, 16)


def test_estado_viaja_como_json():
    st, _ = ix.step(ix.IndexState(), "2026-10", stats(150), prices(150), {}, 1.0, None)
    assert ix.IndexState.from_json(st.to_json()) == st and ix.IndexState.from_json(None) == ix.IndexState()
