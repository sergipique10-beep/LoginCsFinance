"""csgotrader buff163.json → Quote (COST-03).

Forma: {nombre: {"starting_at": {"price": x}, "highest_order": {"price": y}}}. Precio = la oferta
más barata (`starting_at`). La moneda del fichero la resuelve COST-00; aquí llega como
`currency` y, si es CNY, se convierte con `usd_per_cny`. Sin moneda resuelta no se adapta nada.
Las Doppler traen `starting_at.doppler` por fase: se usa el precio general, no el de fase.
"""
import datetime as dt
from typing import Any

from worker.adapters._common import to_float
from worker.models import Quote


class CurrencyUnresolved(Exception):
    """BUFF_CURRENCY sin fijar: la cifra sale del spike COST-00, no se supone."""


def adapt_buff(raw: Any, ts: dt.datetime, currency: str, usd_per_cny: float | None = None) -> list[Quote]:
    if currency not in ("USD", "CNY"):
        raise CurrencyUnresolved(currency or "(vacía)")
    rate = 1.0
    if currency == "CNY":
        if not usd_per_cny:
            raise CurrencyUnresolved("CNY sin tipo de cambio")
        rate = usd_per_cny
    out = []
    for name, entry in (raw.items() if isinstance(raw, dict) else []):
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        price = to_float((entry.get("starting_at") or {}).get("price"))
        out.append(Quote(ts, name, "buff", round(price * rate, 4) if price is not None else None))
    return out
