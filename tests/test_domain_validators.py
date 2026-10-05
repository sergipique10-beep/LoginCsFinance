"""CLEAN-14: validadores de valor de steam/domain/validators.py (as_*).
Las reglas de plausibilidad y de rankings están en tests/test_domain_rules.py (CLEAN-17)."""
import pytest

from steam.domain import validators as v




@pytest.mark.parametrize("point, ok", [({"price": 1}, True), ({"price": 0}, False), ({}, False)])
def test_has_price(point, ok):
    assert v.has_price(point) is ok
