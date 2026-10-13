"""COST-03 · Precio de referencia (worker/pricing/reference.py) y adapters de las fuentes."""
import datetime as dt
import math

import pytest

from worker.adapters import catalog, csgotm, csgotrader, fx, skinport
from worker.adapters._common import to_float, to_int
from worker.models import Quote
from worker.pricing import reference as ref

NOW = dt.datetime(2026, 10, 13, 12, tzinfo=dt.UTC)


def q(price, source="skinport", age_h=0.0, volume=None, listings=None, name="AK"):
    return Quote(NOW - dt.timedelta(hours=age_h), name, source, price, volume, listings)


def test_mediana_ponderada():
    assert ref.weighted_median([(1, 1), (2, 1), (3, 1)]) == 2
    assert ref.weighted_median([(1, 1), (2, 1)]) == 1.5          # empate exacto: media
    assert ref.weighted_median([(1, 10), (2, 1), (3, 1)]) == 1    # el peso manda
    assert ref.weighted_median([(5, 0), (7, 2)]) == 7             # peso 0 no cuenta
    with pytest.raises(ValueError):
        ref.weighted_median([(1, 0)])


def test_peso_log_decay_y_k():
    base = ref.weight(q(1, volume=9), NOW)
    assert math.isclose(base, math.log1p(9))
    assert math.isclose(ref.weight(q(1, volume=9, age_h=6), NOW), base / 2)    # vida media 6 h
    assert math.isclose(ref.weight(q(1, volume=9, age_h=12), NOW), base / 4)
    assert math.isclose(ref.weight(q(1, volume=9, source="buff"), NOW, {"buff": 2}), 2 * base)
    assert math.isclose(ref.weight(q(1), NOW), math.log1p(1))                   # qty mínima 1


def test_una_fuente():
    r = ref.compute("AK", [q(10.0, listings=5)], NOW)
    assert (r.ref_price_usd, r.ref_confidence, r.sources) == (10.0, 1, 1)
    assert r.prices[0]["used"] is True


def test_dos_fuentes_pesa_mas_la_de_volumen():
    r = ref.compute("AK", [q(10.0, "skinport", listings=1), q(12.0, "skinport_sales", volume=500)], NOW)
    assert r.ref_price_usd == 12.0 and r.ref_confidence == 2 and r.volume_24h == 500
    assert r.turnover_24h == 6000.0


def test_outliers_fuera_de_m_medios_a_2m():
    quotes = [q(10.0, "a"), q(11.0, "b"), q(9.0, "c"), q(100.0, "d"), q(1.0, "e")]
    r = ref.compute("AK", quotes, NOW)
    assert r.ref_price_usd == 10.0 and r.sources == 3 and r.ref_confidence == 3
    used = {p["market"]: p["used"] for p in r.prices}
    assert used == {"a": True, "b": True, "c": True, "d": False, "e": False}


def test_decay_hace_ganar_a_la_reciente():
    r = ref.compute("AK", [q(10.0, "a", age_h=48, volume=100), q(12.0, "b", age_h=0, volume=100)], NOW)
    assert r.ref_price_usd == 12.0
    assert r.ref_ts == NOW


def test_sin_precio():
    r = ref.compute("AK", [q(None, volume=3), q(0.0)], NOW)
    assert r.ref_price_usd is None and r.ref_confidence == 0 and r.turnover_24h is None
    assert len(r.prices) == 2


def test_k_a_cero_cae_a_mediana_simple():
    r = ref.compute("AK", [q(10.0, "a"), q(12.0, "b")], NOW, {"a": 0, "b": 0})
    assert r.ref_price_usd == 11.0


def test_market_k():
    assert ref.market_k("") == {}
    assert ref.market_k('{"buff": 1.1}') == {"buff": 1.1}


def test_conversiones_tolerantes():
    assert to_float("1.5") == 1.5 and to_float("x") is None and to_float(-1) is None
    assert to_float(float("nan")) is None and to_float(True) is None and to_int("3.0") == 3


def test_adapter_skinport():
    items = [{"market_hash_name": "AK", "currency": "USD", "min_price": 9.5, "quantity": 12},
             {"market_hash_name": "M4", "currency": "EUR", "min_price": 1},
             {"market_hash_name": "", "min_price": 1}, "basura"]
    out = skinport.adapt_items(items, NOW)
    assert out == [Quote(NOW, "AK", "skinport", 9.5, None, 12)]
    sales = [{"market_hash_name": "AK", "currency": "USD", "last_24_hours": {"median": 10, "volume": 4},
              "last_7_days": {"median": 11}},
             {"market_hash_name": "M4", "currency": "USD", "last_24_hours": {"median": None, "volume": 0},
              "last_7_days": {"median": 3.2}}]
    a, m = skinport.adapt_sales(sales, NOW)
    assert (a.price_usd, a.volume, a.source) == (10.0, 4, "skinport_sales")
    assert (m.price_usd, m.volume) == (3.2, 0)
    assert skinport.adapt_items({"no": "lista"}, NOW) == []


def test_adapter_csgotm():
    raw = {"success": True, "currency": "USD", "items": [{"market_hash_name": "AK", "price": "9.99", "volume": "7"}]}
    assert csgotm.adapt_prices(raw, NOW) == [Quote(NOW, "AK", "marketcsgo", 9.99, None, 7)]
    assert csgotm.adapt_prices({**raw, "currency": "RUB"}, NOW) == []


def test_adapter_buff_moneda():
    raw = {"AK": {"starting_at": {"price": 70}, "highest_order": {"price": 60}}, "X": "raro"}
    assert csgotrader.adapt_buff(raw, NOW, "USD")[0].price_usd == 70
    assert csgotrader.adapt_buff(raw, NOW, "CNY", usd_per_cny=0.14)[0].price_usd == 9.8
    for cur, rate in (("", None), ("EUR", None), ("CNY", None)):
        with pytest.raises(csgotrader.CurrencyUnresolved):
            csgotrader.adapt_buff(raw, NOW, cur, rate)


def test_adapter_catalogo():
    raw = [{"market_hash_name": "AK | Redline (FT)", "weapon": {"name": "AK-47"}, "category": {"name": "Rifles"},
            "rarity": {"name": "Classified", "color": "#d32ce6"}, "image": "https://img"},
           {"market_hash_name": "AK | Redline (FT)"}, {"name": "sin hash"}]
    (c,) = catalog.adapt_catalog(raw)
    assert (c.weapon, c.category, c.rarity, c.rarity_color, c.image_url) == ("AK-47", "Rifles", "Classified", "#d32ce6", "https://img")


def test_adapter_fx():
    assert fx.usd_per_cny({"rates": {"CNY": 7.1}}) == round(1 / 7.1, 6)
    assert fx.usd_per_cny({"rates": {"CNY": 70}}) is None and fx.usd_per_cny(None) is None
