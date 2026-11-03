"""Reglas de los rankings de Home: cajas más vendidas y skins que más suben/bajan.

Sustituyen a HOT/COLD, que puntuaba solo las 20 skins de más facturación y las
repartía 10/10 se hubieran movido o no (spec: docs/superpowers/specs/2026-11-03-home-rankings-design.md).

- **Precio real, nunca la última venta.** `pricelatestsell` es UNA venta: una compra
  suelta a 342 $ de una skin de 0,03 $ se convertía en su precio. Los cambios salen de
  la familia `pricereal` (mercados de terceros), la única con históricos por timeframe.
- **30 días**: con 7 el precio de referencia traía más basura.
- **En dólares**: es lo que mueve dinero; el % va al lado en la tarjeta.
- **Sin relleno**: suben solo con cambio > 0 y bajan solo con < 0. Nunca un 0 %.
- **Mínimo de ventas**: con 1 venta a la semana el precio de una skin cara es una compra
  rara (Souvenir AWP Printstream WW, +792 $ con una venta). 5 deja pasar las caras reales.
"""
from typing import Literal

from steam.domain.models import SteamItem
from steam.domain.rules import plausible_ratio

HOME_RANKING_LIMIT = 10
MIN_SOLD_7D = 5
# Segunda petición a /items: la primera va por unidades y no trae ninguna skin ≥ 500 $.
PRICE_REAL_MIN_EXPENSIVE = 50


def is_case(item: SteamItem) -> bool:
    return item.name.endswith(" Case")


def merge_universe(*batches: list[SteamItem]) -> list[SteamItem]:
    """Une las respuestas de /items sin repetir skin; gana la primera aparición."""
    seen: dict[str, SteamItem] = {}
    for batch in batches:
        for item in batch:
            seen.setdefault(item.name, item)
    return list(seen.values())


def change_30d(item: SteamItem) -> tuple[float, float] | None:
    """(dólares, %) entre `pricereal30d` y `pricereal`, sin redondear. None si falta un
    precio o si están a más de 10× (basura de la API, no un movimiento)."""
    now, then = item.price_real, item.price_real_30d
    if not now or not then or not plausible_ratio(now, then):
        return None
    return now - then, (now - then) / then * 100


def top_cases(items: list[SteamItem]) -> list[SteamItem]:
    cases = [i for i in items if is_case(i) and (i.sold_24h or 0) > 0]
    return sorted(cases, key=lambda i: i.sold_24h or 0, reverse=True)[:HOME_RANKING_LIMIT]


def top_movers(items: list[SteamItem],
               direction: Literal["up", "down"]) -> list[tuple[SteamItem, float, float]]:
    """Las skins (todo menos cajas) que más suben o bajan en dólares a 30 días."""
    rows = []
    for item in items:
        if is_case(item) or (item.sold_7d or 0) < MIN_SOLD_7D:
            continue
        change = change_30d(item)
        if change is None:
            continue
        usd, pct = change
        if (usd > 0) if direction == "up" else (usd < 0):
            rows.append((item, usd, pct))
    rows.sort(key=lambda r: r[1], reverse=direction == "up")
    return rows[:HOME_RANKING_LIMIT]


def usd_from_pct(price_now: float | None, pct: float | None) -> float | None:
    """Cambio en dólares a partir del precio actual y del % (se guarda el %, no el $)."""
    if price_now is None or pct is None or pct == -100:
        return None
    return price_now - price_now / (1 + pct / 100)
