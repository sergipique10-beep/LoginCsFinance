"""Reglas de plausibilidad de steam/ (CLEAN-12). Son las que ya existían sueltas por el
código, reunidas con nombre y test; ninguna es nueva. Endurecerlas (p. ej. tratar un
precio implausible como inválido) cambia lo que ve el usuario: es UX-46.
"""
from collections.abc import Mapping
from typing import Any

from typing_extensions import TypeIs

from steam.errors import InvalidField

# ── Validadores de valor (CLEAN-14) ───────────────────────────────────────────
# Sustituyen a `float(x or 0)` / `int(x or 0)` en los adapters. La regla: un campo
# ausente, vacío o no convertible es `None` (no `0`, no `""`); un valor de un tipo
# imposible (un dict donde va un número) es `InvalidField`, porque eso ya no es un
# dato sucio sino otra forma de payload. `0` y `False` son datos y se conservan.

_CONTAINER = (dict, list, tuple, set)


def as_float(value: Any, *, field: str = "", source: str = "", op: str = "") -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, _CONTAINER):
        raise InvalidField(source, op, field, value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any, *, field: str = "", source: str = "", op: str = "") -> int | None:
    number = as_float(value, field=field, source=source, op=op)
    return None if number is None else int(number)


def as_bool(value: Any, *, field: str = "", source: str = "", op: str = "") -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, _CONTAINER):
        raise InvalidField(source, op, field, value)
    return bool(value)


def as_str(value: Any, *, field: str = "", source: str = "", op: str = "") -> str | None:
    """None si falta; los números se admiten como texto (ids); un contenedor es inválido."""
    if value is None:
        return None
    if isinstance(value, _CONTAINER):
        raise InvalidField(source, op, field, value)
    return value if isinstance(value, str) else str(value)


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
    """Precio y ventas mínimos de un item crudo de /items para entrar en un ranking.
    Pasa a recibir `SteamItem` cuando los services usen el adapter (siguiente commit)."""
    latest = as_float(raw.get("pricelatestsell"), field="pricelatestsell", source="steamwebapi", op="items") or 0
    volume = as_int(raw.get("sold24h"), field="sold24h", source="steamwebapi", op="items") or 0
    return latest >= MIN_RANKING_PRICE and volume >= min_sold


def canonical_price(item: Mapping[str, Any]) -> float | None:
    """Precio canónico: pricelatestsell → pricelatest → pricemedian (primero > 0).

    Sobre el dict crudo de `/item`: lo consumen price_capture y alerts, que pasan al
    modelo interno en la Fase 5 del refactor (price_capture_service)."""
    for key in ("pricelatestsell", "pricelatest", "pricemedian"):
        v = as_float(item.get(key), field=key, source="steamwebapi", op="item")
        if v is not None and v > 0:
            return v
    return None


def has_price(point: Mapping[str, Any]) -> bool:
    """Un punto de histórico sin precio (o a 0) no es un punto: se descarta."""
    return bool(point.get("price"))
