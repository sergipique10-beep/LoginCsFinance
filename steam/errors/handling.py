"""Manejo de errores y degradaciones de steam/.

- `http_error_for`: la traducción ÚNICA de los errores tipados a HTTP. Las rutas
  capturan `SOURCE_ERRORS` y lanzan lo que devuelve; con un bloque `except` por ruta los
  status divergían (un 200 ilegible daba 500: CAL-14).
- `log_degraded`: una línea con formato fijo cada vez que se sirve un dato degradado que
  el cliente no ve (caducado, vacío o de respaldo), y el conteo de la última hora para
  ese (flow, reason). Mismo patrón que `[inventory-429]`.
- `served_to_status`: de lo que se sirvió (`Served`) al `FetchStatus` de `Fetched`, en un
  solo sitio.

Este módulo sí importa FastAPI: no es un service (la guardia de capas excluye `errors/`).
Buscar en los logs de Render: `grep steam-degraded`, o `grep "flow=movers"`.
"""
import logging
import math
import time
from collections import deque
from typing import TypeVar

from fastapi import HTTPException

from steam.domain.enums import FetchStatus, Served
from steam.domain.models import Fetched
from steam.errors.domain_errors import (
    UNEXPECTED_FORMAT, UPSTREAM_QUOTA_DETAIL, UPSTREAM_RATE_LIMIT_DETAIL, HistoryBusy, InvalidField,
    InvalidPayload, QuotaExhausted, RateLimited, SourceTimeout, SourceUnavailable, StorageError,
    UnexpectedPayload, UpstreamError,
)

logger = logging.getLogger("uvicorn.error")

_T = TypeVar("_T")

# Lo que lanzan api/, adapters/ y services/ y una ruta debe traducir. Lo que no esté aquí
# (un KeyError, un TypeError) es un bug y debe salir como 500, no disfrazado de 502.
SOURCE_ERRORS = (UpstreamError, InvalidPayload, UnexpectedPayload, HistoryBusy)
# Lo que un service captura cuando la fuente falla y tiene un camino de degradación
# (stale, respaldo, vacío). `HistoryBusy` no está: es del llamador que no puede esperar.
DEGRADABLE = (UpstreamError, InvalidPayload, UnexpectedPayload)

# SEC-16: segundos de `Retry-After` cuando steamwebapi no lo dice (ventana de 60 s).
RETRY_AFTER_DEFAULT = 60

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
    if isinstance(exc, InvalidField):
        return "invalid_field"
    if isinstance(exc, UnexpectedPayload):
        return "unexpected_format"
    if isinstance(exc, StorageError):
        return "storage"
    return type(exc).__name__


def http_error_for(exc: BaseException, *, timeout_status: int = 502) -> HTTPException:
    """El `HTTPException` que corresponde a un error tipado.

    - `QuotaExhausted` (402, cuota mensual) → 503 `UPSTREAM_QUOTA_DETAIL` (SEC-16).
    - `RateLimited` (429) y `HistoryBusy` (limiter lleno) → 503 `UPSTREAM_RATE_LIMIT_DETAIL`
      con `Retry-After` (el de steamwebapi si lo dio; si no, 60).
    - `SourceTimeout` → `timeout_status`: 504 en casi todas las rutas, 502 en la búsqueda
      (/market/items y /market/price siempre respondieron 502 al timeout).
    - `SourceUnavailable` (red) y cualquier otro `UpstreamError` → 502.
    - `InvalidPayload` (200 ilegible), `UnexpectedPayload` e `InvalidField` → 502 con el
      motivo en `detail`: un 200 ilegible es un fallo de la fuente, no un 500 (CAL-14).

    Lo que no sea `SOURCE_ERRORS` no es un fallo de la fuente: se devuelve un 502 genérico
    para no perderlo, pero quien llama debería capturar solo `SOURCE_ERRORS`.
    """
    if isinstance(exc, QuotaExhausted):
        return HTTPException(status_code=503, detail=UPSTREAM_QUOTA_DETAIL)
    if isinstance(exc, (RateLimited, HistoryBusy)):
        retry_after = getattr(exc, "retry_after", None)
        seconds = math.ceil(retry_after) if retry_after else RETRY_AFTER_DEFAULT
        return HTTPException(status_code=503, detail=UPSTREAM_RATE_LIMIT_DETAIL,
                             headers={"Retry-After": str(seconds)})
    if isinstance(exc, SourceTimeout):
        return HTTPException(status_code=timeout_status, detail="Steam request timed out")
    if isinstance(exc, SourceUnavailable):
        return HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")
    if isinstance(exc, UpstreamError):
        return HTTPException(status_code=502, detail=f"Steam returned {exc.status}")
    if isinstance(exc, InvalidPayload):
        return HTTPException(status_code=502, detail=UNEXPECTED_FORMAT)
    return HTTPException(status_code=502, detail=str(exc) or UNEXPECTED_FORMAT)


def user_message(exc: BaseException) -> str:
    """Motivo legible de un error de la fuente, para que el modelo del chat lo explique
    (CAL-14: sin motivo, el chat decía «error al ejecutar» o devolvía un `[]`)."""
    if isinstance(exc, QuotaExhausted):
        return "la cuota mensual de steamwebapi está agotada; los datos de mercado volverán el día 10"
    if isinstance(exc, (RateLimited, HistoryBusy)):
        return "steamwebapi ha alcanzado su límite por minuto; inténtalo en un momento"
    if isinstance(exc, SourceTimeout):
        return "Steam API no respondió a tiempo"
    if isinstance(exc, SourceUnavailable):
        return "no se pudo contactar con Steam API"
    if isinstance(exc, UpstreamError):
        if exc.status == 403:
            return "el inventario de Steam es privado"
        return f"Steam API devolvió un error {exc.status}"
    if isinstance(exc, (InvalidPayload, UnexpectedPayload)):
        return "formato inesperado de Steam API"
    return f"error inesperado consultando Steam API: {type(exc).__name__}"


def served_to_status(served: Served) -> FetchStatus:
    """`Fetched.status` a partir de lo que se sirvió: un dato caducado es `stale`, un
    respaldo es `partial`, y un vacío o un fallo es `error`."""
    if served == "stale":
        return "stale"
    if served == "fallback":
        return "partial"
    return "error"


def log_degraded(flow: str, reason: str, served: Served) -> None:
    """`[steam-degraded] flow= reason= served= last_hour=`, a WARNING."""
    now = time.time()
    window = _recent.setdefault((flow, reason), deque())
    window.append(now)
    while window[0] < now - 3600:
        window.popleft()
    logger.warning("[steam-degraded] flow=%s reason=%s served=%s last_hour=%d",
                   flow, reason, served, len(window))


def degraded(flow: str, reason: str, served: Served, data: _T) -> Fetched[_T]:
    """La línea de degradación y el `Fetched` que la acompaña, de una vez: el `status`
    sale de `served` por `served_to_status`, así que ningún service lo decide a mano."""
    log_degraded(flow, reason, served)
    return Fetched(data, served_to_status(served), reason)
