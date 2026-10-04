"""CLEAN-12: reglas de plausibilidad que ya existían, reunidas en steam/domain/validators.py.
Ninguna regla nueva: cada caso fija el comportamiento de antes."""
import pytest

from steam.domain import validators as v


@pytest.mark.parametrize("new, old, ok", [
    (10.0, 10.0, True), (10.0, 1.0, True), (10.0, 100.0, True),   # bordes inclusivos (10×)
    (10.0, 0.99, False), (10.0, 100.1, False),
])
def test_plausible_ratio(new, old, ok):
    assert v.plausible_ratio(new, old) is ok


@pytest.mark.parametrize("rate, ok", [
    (0.88, True), (1, True), (0.5, False), (2.0, False), (0.0, False), (None, False), ("0.88", False),
])
def test_plausible_fx_rate(rate, ok):
    assert v.plausible_fx_rate(rate) is ok


@pytest.mark.parametrize("price, sold, min_sold, ok", [
    (10.80, 5, v.MIN_SOLD_MOVERS, True), (10.79, 500, v.MIN_SOLD_MOVERS, False),
    (50.0, 4, v.MIN_SOLD_MOVERS, False), (50.0, 1, v.MIN_SOLD_TRENDING, True),
    (None, None, v.MIN_SOLD_TRENDING, False),
])
def test_ranking_eligible(price, sold, min_sold, ok):
    assert v.ranking_eligible({"pricelatestsell": price, "sold24h": sold}, min_sold) is ok


@pytest.mark.parametrize("item, price", [
    ({"pricelatestsell": 5, "pricelatest": 7}, 5.0),
    ({"pricelatestsell": 0, "pricelatest": "7.5"}, 7.5),
    ({"pricelatestsell": "x", "pricemedian": 3}, 3.0),
    ({}, None),
])
def test_canonical_price(item, price):
    assert v.canonical_price(item) == price


@pytest.mark.parametrize("point, ok", [({"price": 1}, True), ({"price": 0}, False), ({}, False)])
def test_has_price(point, ok):
    assert v.has_price(point) is ok
