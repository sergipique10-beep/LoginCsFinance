"""UX-46: el estado de un `Fetched` viaja al front en cabeceras, no en el cuerpo.

Los tests de contrato fijan las claves exactas de cada cuerpo y varios cuerpos son
listas, así que el estado no cabe dentro sin romper el contrato. Es el mismo patrón que
`X-Inventory-*` (PERF-14). Con `status == "ok"` no se emite ninguna cabecera: su
ausencia significa «dato bueno». `X-Data-Reason` es el mismo string que
`[steam-degraded] reason=`, para cruzar lo que ve el usuario con los logs.
"""
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..domain.enums import FetchStatus
from ..domain.models import Fetched

DATA_STATUS = "X-Data-Status"
DATA_REASON = "X-Data-Reason"
DATA_CAPTURED_AT = "X-Data-Captured-At"
# Las que `main.py` expone por CORS: sin eso el WebView de Capacitor no las lee.
DATA_HEADERS = (DATA_STATUS, DATA_REASON, DATA_CAPTURED_AT)

_FROM_FETCHED = object()


def data_headers(status: FetchStatus, reason: str | None,
                 captured_at: str | None = None) -> dict[str, str]:
    if status == "ok":
        return {}
    headers = {DATA_STATUS: status, DATA_REASON: reason or "unknown"}
    if captured_at:
        headers[DATA_CAPTURED_AT] = captured_at
    return headers


def respond(fetched: Fetched[Any], body: Any = _FROM_FETCHED,
            captured_at: str | None = None) -> Any:
    """`body` (por defecto `fetched.data`) tal cual si el dato es bueno; si no, el mismo
    cuerpo con las cabeceras de estado."""
    payload = fetched.data if body is _FROM_FETCHED else body
    headers = data_headers(fetched.status, fetched.reason, captured_at)
    if not headers:
        return payload
    return JSONResponse(jsonable_encoder(payload), headers=headers)
