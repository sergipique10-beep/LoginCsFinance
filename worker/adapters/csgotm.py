"""market.csgo.com prices/USD.json → Quote (COST-03). `volume` del feed = ofertas abiertas."""
import datetime as dt
from typing import Any

from worker.adapters._common import name_of, to_float, to_int
from worker.models import Quote


def adapt_prices(raw: Any, ts: dt.datetime) -> list[Quote]:
    if not isinstance(raw, dict) or raw.get("currency", "USD") != "USD":
        return []
    out = []
    for row in raw.get("items") or []:
        name = name_of(row)
        if name:
            out.append(Quote(ts, name, "marketcsgo", to_float(row.get("price")),
                             listings=to_int(row.get("volume"))))
    return out
