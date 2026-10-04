"""Adapter del histórico de Buff163 (`/market/buff/history`): puntos con `quantity`."""
from typing import Any

from steam.adapters._common import history_points
from steam.domain.models import HistoryPoint

SOURCE = "buff"


def adapt_history(raw: Any) -> list[HistoryPoint]:
    return history_points(raw, "quantity", source=SOURCE, op="history")
