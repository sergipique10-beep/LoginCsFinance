"""Reglas de plausibilidad de steam/ (CLEAN-12). Son las que ya existían sueltas por el
código, reunidas con nombre y test; ninguna es nueva. Endurecerlas (p. ej. tratar un
precio implausible como inválido) cambia lo que ve el usuario: es UX-46.
"""
from collections.abc import Mapping
from typing import Any

from typing_extensions import TypeIs

# Un precio histórico fuera de este rango respecto al actual es basura de la API
# (visto: pricereal30d=0.22 para una skin de 17.57 → +7886%), no un movimiento real.
MAX_PLAUSIBLE_RATIO = 10.0

# Un tipo USD/EUR fuera de este rango es un error de la fuente, no un movimiento de
# mercado: mejor servir el último bueno que corromper precios (UX-08).
FX_MIN, FX_MAX = 0.5, 2.0

# Suelo de precio para entrar en los rankings (USD; ~10 EUR a 1.08 USD/EUR).
# Por debajo, el movimiento porcentual es ruido de granularidad: los precios de
# Steam se mueven de centavo en centavo, así que en un item de $0.10 un solo tick
# ya es un +10% que ni el spread ni las comisiones (~15%) dejan capturar. Medido
# en producción: el 61% del trending eran items de ~$0.11 con volatilidad
# aparente 3.6x la de los de $2-10, y el agente los presentaba como "en auge".
MIN_RANKING_PRICE = 10.80
# Ventas en 24 h para entrar: los movers piden más que el trending.
MIN_SOLD_MOVERS = 5
MIN_SOLD_TRENDING = 1


def plausible_ratio(new: float, old: float) -> bool:
    """¿Está el precio viejo a menos de MAX_PLAUSIBLE_RATIO veces del nuevo? (ambos > 0)"""
    return 1 / MAX_PLAUSIBLE_RATIO <= old / new <= MAX_PLAUSIBLE_RATIO


def plausible_fx_rate(rate: Any) -> TypeIs[float]:
    return isinstance(rate, (int, float)) and FX_MIN < rate < FX_MAX


def ranking_eligible(raw: Mapping[str, Any], min_sold: int) -> bool:
    """Precio y ventas mínimos de un item crudo de /items para entrar en un ranking."""
    latest = float(raw.get("pricelatestsell") or 0)
    volume = int(raw.get("sold24h") or 0)
    return latest >= MIN_RANKING_PRICE and volume >= min_sold


def canonical_price(item: Mapping[str, Any]) -> float | None:
    """Precio canónico: pricelatestsell → pricelatest → pricemedian (primero > 0)."""
    for key in ("pricelatestsell", "pricelatest", "pricemedian"):
        try:
            v = float(item.get(key) or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            return v
    return None


def has_price(point: Mapping[str, Any]) -> bool:
    """Un punto de histórico sin precio (o a 0) no es un punto: se descarta."""
    return bool(point.get("price"))
