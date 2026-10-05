import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from steam.cache.user_cache import _inventory_refresh_cooldown
from auth.service import item_history_rate_limit, require_jwt
from ..domain.models import Fetched
from ..errors.handling import SOURCE_ERRORS, http_error_for
from ..errors import UPSTREAM_QUOTA_DETAIL, UpstreamError
from ..services import inventory_service, pricing_service, profile_service
from ..services.inventory_service import Inventory

# SEC-16: espera máxima por un hueco en `_history_limiter` (como el chat en PERF-03).
# Un detalle de skin no puede quedarse 60 s cargando mientras un cron llena la ventana.
ITEM_HISTORY_LIMITER_TIMEOUT = 3.0

logger = logging.getLogger("uvicorn.error")
router = APIRouter()


@router.get("/me", summary="Info del usuario autenticado")
async def get_me(request: Request, user: dict = Depends(require_jwt)):
    steam_id: str = user["sub"]
    try:
        return (await profile_service.get_profile(request.app.state.http_client, steam_id)).data
    except SOURCE_ERRORS as exc:
        raise http_error_for(exc, timeout_status=504) from exc


def _inventory_http_error(exc: Exception) -> HTTPException:
    """Errores de la fuente que `inventory_service` deja subir (PERF-14 degrada solo 429/402)."""
    if isinstance(exc, UpstreamError):
        if exc.status == 403:
            return HTTPException(status_code=403, detail="Inventory is private")
        if exc.status is not None:
            logger.error("[inventory] steamwebapi /inventory → %s: %.500s", exc.status, exc.body_excerpt)
    return http_error_for(exc, timeout_status=504)


def _inventory_response(fetched: Fetched[Inventory]) -> JSONResponse | list:
    """Cuerpo = la lista; dato viejo en cabeceras (PERF-14); sin snapshot 429 o 503 (SEC-16)."""
    if fetched.status == "error" and fetched.reason == "rate_limit":
        raise HTTPException(status_code=429, detail="Steam rate limit — retry later")
    if fetched.status == "error" and fetched.reason == "quota":
        raise HTTPException(status_code=503, detail=UPSTREAM_QUOTA_DETAIL)
    if fetched.status == "stale":
        return JSONResponse(fetched.data.items, headers={
            "X-Inventory-Stale": "1", "X-Inventory-Captured-At": fetched.data.captured_at or "",
        })
    return fetched.data.items   # `ok`, o `error` por 410/411: `[]` (CAL-13)


@router.get("/inventory", summary="Inventario CS2 del usuario autenticado")
async def get_inventory(request: Request, user: dict = Depends(require_jwt)):
    try:
        fetched = await inventory_service.get_inventory(request.app.state.http_client, user["sub"], origin="get")
    except SOURCE_ERRORS as exc:
        raise _inventory_http_error(exc) from exc
    return _inventory_response(fetched)


@router.post("/inventory/refresh", summary="Fuerza un refresh del inventario ignorando el caché de 23h")
async def refresh_inventory(request: Request, user: dict = Depends(require_jwt)):
    steam_id: str = user["sub"]
    now = time.monotonic()
    if _inventory_refresh_cooldown.fresh(steam_id, now) is not None:
        remaining = int(_inventory_refresh_cooldown.ttl - (now - _inventory_refresh_cooldown[steam_id][1]))
        raise HTTPException(status_code=429, detail=f"Refresh cooldown active — retry in {remaining}s")
    try:
        fetched = await inventory_service.get_inventory(request.app.state.http_client, steam_id,
                                                        force=True, origin="refresh")
    except SOURCE_ERRORS as exc:
        raise _inventory_http_error(exc) from exc
    if fetched.status == "ok":
        _inventory_refresh_cooldown.put(steam_id, True, now)
    return _inventory_response(fetched)


@router.get("/item/history", dependencies=[Depends(item_history_rate_limit)],
            summary="Historial de precios de un item CS2")
async def get_item_history(
    request: Request,
    name: str,
    interval: str = "10",
    market: str | None = None,
    days: int = 35,
    user: dict = Depends(require_jwt),
):
    try:
        fetched = await pricing_service.get_item_history(
            request.app.state.http_client, name, interval, market, days,
            limiter_timeout=ITEM_HISTORY_LIMITER_TIMEOUT,
        )
    except SOURCE_ERRORS as exc:
        # Ventana llena / 429 sin caché caducada → 503 + Retry-After; 402 → 503 upstream_quota.
        raise http_error_for(exc, timeout_status=504) from exc
    return fetched.data
