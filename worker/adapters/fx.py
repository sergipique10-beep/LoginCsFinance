"""frankfurter /latest?from=USD&to=CNY → USD por CNY (COST-03)."""
from typing import Any

from worker.adapters._common import to_float


def usd_per_cny(raw: Any) -> float | None:
    cny = to_float(((raw or {}).get("rates") or {}).get("CNY")) if isinstance(raw, dict) else None
    # Plausibilidad: 1 USD lleva años entre 6 y 8 CNY. Fuera de eso, no se usa.
    return round(1 / cny, 6) if cny and 5 < cny < 10 else None
