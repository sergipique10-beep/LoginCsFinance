"""priceoverview de Steam → Quote (COST-04).

Steam devuelve textos con moneda: `lowest_price: "$12.34"`, `volume: "1,234"`. Con `currency=1`
la moneda es USD y el separador de miles la coma. `success: false` o sin precio → None.
source `steam`: precio = `lowest_price` (la oferta más barata, como `min_price` de Skinport),
`volume` = ventas de 24 h. `median_price` no se guarda: la referencia pondera ofertas y ventas
por fuente, no dos precios de la misma fuente (ponytail: se añade si la liquidez lo pide).
"""
import datetime as dt
import re
from typing import Any

from worker.models import Quote

_NUM = re.compile(r"[\d.,]+")


def parse_money(v: Any) -> float | None:
    """"$1,234.56" → 1234.56. Solo formato USD de Steam (miles con coma, decimales con punto)."""
    if not isinstance(v, str):
        return None
    m = _NUM.search(v)
    if not m:
        return None
    try:
        f = float(m.group().replace(",", ""))
    except ValueError:
        return None
    return f if f >= 0 else None


def adapt_priceoverview(raw: Any, market_hash_name: str, ts: dt.datetime) -> Quote | None:
    if not isinstance(raw, dict) or not raw.get("success"):
        return None
    price = parse_money(raw.get("lowest_price"))
    if price is None:
        return None
    vol = parse_money(raw.get("volume"))
    return Quote(ts, market_hash_name, "steam", price, volume=int(vol) if vol is not None else None)
