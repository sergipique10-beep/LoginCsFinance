"""Conversión tolerante de campos de las fuentes: un campo raro vale None, no tumba el lote."""
from typing import Any


def to_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f >= 0 else None  # NaN y negativos fuera


def to_int(v: Any) -> int | None:
    f = to_float(v)
    return int(f) if f is not None else None


def name_of(row: Any) -> str | None:
    n = row.get("market_hash_name") if isinstance(row, dict) else None
    return n.strip() if isinstance(n, str) and n.strip() else None
