"""Fechas: el día de hoy (inyectable en tests), el día ISO de un timestamp y el floor a
la hora. Sin dependencias internas."""
from datetime import date, datetime


def today() -> date:
    """`date.today()` detrás de una función: un test lo sustituye con monkeypatch en vez
    de congelar el módulo `datetime` entero."""
    return date.today()


def iso_day(value: str | None) -> str:
    """Los 10 primeros caracteres de un timestamp ISO (`2026-10-05T07:00:00Z` → `2026-10-05`);
    "" si falta."""
    return (value or "")[:10]


def hour_floor(moment: datetime) -> datetime:
    """El instante al inicio de su hora (minutos, segundos y micros a cero)."""
    return moment.replace(minute=0, second=0, microsecond=0)
