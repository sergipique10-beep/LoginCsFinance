"""Skinport /v1/items y /v1/sales/history → Quote (COST-03).

- items: precio = `min_price` (la oferta más barata), `listings` = `quantity`. source `skinport`.
- sales/history: precio = mediana de 24 h si hubo ventas, si no la de 7 días; `volume` = ventas
  de 24 h. source `skinport_sales` (otra fuente: ventas, no ofertas).
Ambos piden `currency=USD`; una fila en otra moneda se descarta.
"""
import datetime as dt
from typing import Any

from worker.adapters._common import name_of, to_float, to_int
from worker.models import Quote


def adapt_items(raw: Any, ts: dt.datetime) -> list[Quote]:
    out = []
    for row in raw if isinstance(raw, list) else []:
        name = name_of(row)
        if not name or row.get("currency", "USD") != "USD":
            continue
        out.append(Quote(ts, name, "skinport", to_float(row.get("min_price")),
                         listings=to_int(row.get("quantity"))))
    return out


def adapt_sales(raw: Any, ts: dt.datetime) -> list[Quote]:
    out = []
    for row in raw if isinstance(raw, list) else []:
        name = name_of(row)
        if not name or row.get("currency", "USD") != "USD":
            continue
        d1 = row.get("last_24_hours") or {}
        d7 = row.get("last_7_days") or {}
        vol = to_int(d1.get("volume"))
        price = to_float(d1.get("median")) if vol else to_float(d7.get("median"))
        out.append(Quote(ts, name, "skinport_sales", price, volume=vol or 0))
    return out
