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


def sales_stats(raw: Any) -> dict[str, dict[str, Any]]:
    """Resumen por nombre de /v1/sales/history para rankings e índice (COST-05): volúmenes y medianas
    de 24 h, 7 d y 30 d, y la media de 30 d. Es lo que `SalesStats` (worker/pricing/index.py) recibe."""
    out: dict[str, dict[str, Any]] = {}
    for row in raw if isinstance(raw, list) else []:
        name = name_of(row)
        if not name or row.get("currency", "USD") != "USD":
            continue
        d1, d7, d30 = (row.get(k) or {} for k in ("last_24_hours", "last_7_days", "last_30_days"))
        out[name] = {"volume_24h": to_int(d1.get("volume")) or 0, "volume_7d": to_int(d7.get("volume")) or 0,
                     "volume_30d": to_int(d30.get("volume")) or 0, "median_24h": to_float(d1.get("median")),
                     "median_7d": to_float(d7.get("median")), "median_30d": to_float(d30.get("median")),
                     "avg_30d": to_float(d30.get("avg"))}
    return out
