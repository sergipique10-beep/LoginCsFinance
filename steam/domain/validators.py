"""Validadores de VALOR de steam/: un campo del payload → su tipo, o None.
Las reglas de plausibilidad y de rankings y `canonical_price` viven en
`domain/rules.py`; aquí no se decide nada sobre un ítem, solo qué es y qué no es un número.
"""
from collections.abc import Mapping
from typing import Any

from steam.errors import InvalidField

# ── Validadores de valor ────────────────────────────────────────────────────
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


def has_price(point: Mapping[str, Any]) -> bool:
    """Un punto de histórico sin precio (o a 0) no es un punto: se descarta."""
    return bool(point.get("price"))
