"""Adapter de frankfurter (`/v1/latest?base=USD&symbols=EUR`) → `FxRates`."""
from collections.abc import Mapping
from typing import Any

from steam.adapters._common import require_dict
from steam.domain.models import FxRates
from steam.domain.validators import as_float

SOURCE = "frankfurter"


def adapt_rates(raw: Any) -> FxRates:
    d = require_dict(raw, source=SOURCE, op="latest")
    rates = d.get("rates")
    eur = rates.get("EUR") if isinstance(rates, Mapping) else None
    return FxRates(eur=as_float(eur, field="rates.EUR", source=SOURCE, op="latest"))
