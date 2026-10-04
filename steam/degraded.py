"""Log de degradaciones de steam/ (CLEAN-12): una línea con formato fijo cada vez que
se sirve un dato degradado que el cliente no ve (caducado, vacío o de respaldo), y el
conteo de la última hora para ese (flow, reason). Mismo patrón que `[inventory-429]`.

Buscar en los logs de Render: `grep steam-degraded`, o `grep "flow=movers"`.
"""
import logging
import time
from collections import deque
from typing import Literal

from steam.errors import (
    HistoryBusy, InvalidPayload, QuotaExhausted, RateLimited, SourceTimeout, SourceUnavailable,
    UnexpectedPayload, UpstreamError,
)

logger = logging.getLogger("uvicorn.error")

Served = Literal["stale", "empty", "fallback", "error"]

_recent: dict[tuple[str, str], deque[float]] = {}   # (flow, reason) → time.time() de la última hora


def reason_of(exc: BaseException) -> str:
    """Motivo corto y estable de un error, para agrupar en los logs."""
    if isinstance(exc, SourceTimeout):
        return "timeout"
    if isinstance(exc, SourceUnavailable):
        return "unavailable"
    if isinstance(exc, QuotaExhausted):
        return "quota"
    if isinstance(exc, (RateLimited, HistoryBusy)):
        return "rate_limit"
    if isinstance(exc, UpstreamError):
        return f"http_{exc.status}"
    if isinstance(exc, InvalidPayload):
        return "invalid_json"
    if isinstance(exc, UnexpectedPayload):
        return "unexpected_format"
    return type(exc).__name__


def log_degraded(flow: str, reason: str, served: Served) -> None:
    """`[steam-degraded] flow= reason= served= last_hour=`, a WARNING."""
    now = time.time()
    window = _recent.setdefault((flow, reason), deque())
    window.append(now)
    while window[0] < now - 3600:
        window.popleft()
    logger.warning("[steam-degraded] flow=%s reason=%s served=%s last_hour=%d",
                   flow, reason, served, len(window))
