"""Adapter del histórico de CSFloat (`/market/csfloat/history`): puntos con `quantity`."""
from typing import Any

from steam.adapters._common import history_points
from steam.domain.models import HistoryPoint

SOURCE = "csfloat"


def adapt_history(raw: Any) -> list[HistoryPoint]:
    return history_points(raw, "quantity", source=SOURCE, op="history")
