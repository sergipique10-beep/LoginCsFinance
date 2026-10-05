"""Comprobaciones de forma y mapeos compartidos por los adapters."""
from collections.abc import Mapping
from typing import Any

from steam.domain.models import HistoryPoint
from steam.domain.validators import as_float, as_int, as_str
from steam.errors import UNEXPECTED_FORMAT, UnexpectedPayload
from steam.utils.dates import iso_day


def require_list(raw: Any, *, source: str, op: str) -> list:
    if isinstance(raw, list):
        return raw
    raise UnexpectedPayload(f"{UNEXPECTED_FORMAT}: {source}.{op} expected a list, got {type(raw).__name__}")


def require_dict(raw: Any, *, source: str, op: str) -> Mapping:
    if isinstance(raw, Mapping):
        return raw
    raise UnexpectedPayload(f"{UNEXPECTED_FORMAT}: {source}.{op} expected an object, got {type(raw).__name__}")


def mappings(raw: list, *, source: str, op: str) -> list[Mapping]:
    """Los elementos de la lista, que tienen que ser objetos."""
    for element in raw:
        if not isinstance(element, Mapping):
            raise UnexpectedPayload(
                f"{UNEXPECTED_FORMAT}: {source}.{op} expected objects, got {type(element).__name__}")
    return raw


def history_points(raw: Any, volume_key: str, *, source: str, op: str) -> list[HistoryPoint]:
    """Los puntos de un histórico (`createdat`, `price`, `quantity`|`sold`) ordenados por
    fecha. Un punto sin precio (o a 0) no es un punto y se descarta, como siempre; el
    volumen ausente sale como 0 porque `HistoryPoint.volume` es `int` por contrato.

    Una sola implementación: antes estaba duplicada en `services/pricing.py`.
    """
    points: list[HistoryPoint] = []
    for p in mappings(require_list(raw, source=source, op=op), source=source, op=op):
        price = as_float(p.get("price"), field="price", source=source, op=op)
        if not price:
            continue
        points.append({
            "date": iso_day(as_str(p.get("createdat"), field="createdat", source=source, op=op)),
            "price": price,
            "volume": as_int(p.get(volume_key), field=volume_key, source=source, op=op) or 0,
        })
    return sorted(points, key=lambda p: p["date"])
