"""CLEAN-14: validadores de valor de steam/domain/validators.py (as_* y canonical_price).
Las reglas de plausibilidad y de rankings están en tests/test_domain_rules.py (CLEAN-17)."""
import pytest

from steam.domain import validators as v


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
