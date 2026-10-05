import asyncio
import logging
import random
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from settings import (
    INVENTORY_429_MAX_RETRIES, INVENTORY_429_BACKOFF_BASE, INVENTORY_429_BACKOFF_CAP,
)
from steam.cache.user_cache import _inventory_cache, _inventory_refresh_cooldown
from auth.service import item_history_rate_limit, require_jwt
from .. import inventory_snapshot_repo
from ..domain.models import Fetched
from ..errors.handling import SOURCE_ERRORS, http_error_for, log_degraded
from ..errors import UPSTREAM_QUOTA_DETAIL, QuotaExhausted, RateLimited, StorageError, UpstreamError
from ..services import inventory as inventory_service
from ..services import pricing
from ..services import profile as profile_service


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


async def _fetch_fresh_inventory(request: Request, steam_id: str) -> Fetched[list]:
    """El inventario recién leído, con los errores de steamwebapi traducidos a HTTP.
    429 y 402 suben tal cual: los degrada quien llama (PERF-14). Un 410/411 llega como
    `Fetched` con `status="error"` y lo resuelve `_no_inventory` (CAL-13)."""
    try:
        return await inventory_service.fetch_fresh_inventory(
            request.app.state.http_client, steam_id, track=True,
        )
    except (QuotaExhausted, RateLimited):
        raise
    except UpstreamError as exc:
        if exc.status == 403:
            raise HTTPException(status_code=403, detail="Inventory is private") from exc
        if exc.status is not None:
            logger.error("steamwebapi /inventory → %s: %.500s", exc.status, exc.body_excerpt)
        raise http_error_for(exc, timeout_status=504) from exc
    except SOURCE_ERRORS as exc:
        raise http_error_for(exc, timeout_status=504) from exc


# ── PERF-14: degradación elegante ante 429 ────────────────────────────────────

_retry_tasks: dict[str, asyncio.Task] = {}   # steam_id → reintento en curso (a la vez, uno)
_recent_429: deque[float] = deque()          # time.time() de los 429 de la última hora


def _log_429(steam_id: str, origin: str, retry_after: float | None, served: str) -> None:
    """Una línea por 429, con el conteo de la última hora: es lo que dice si son
    recurrentes (criterio para subir de plan). `grep inventory-429` en los logs."""
    now = time.time()
    _recent_429.append(now)
    while _recent_429[0] < now - 3600:
        _recent_429.popleft()
    logger.warning(
        "[inventory-429] user=%s origin=%s retry_after=%s served=%s last_hour=%d",
        steam_id, origin, retry_after, served, len(_recent_429),
    )


async def _store(steam_id: str, items: list, now: float) -> None:
    _inventory_cache.put(steam_id, items, now)
    try:
        await inventory_snapshot_repo.save(steam_id, items)
    except StorageError as exc:   # best-effort: nunca romper /inventory
        logger.warning("[inventory] no se pudo guardar el snapshot: %s", exc)
        log_degraded("inventory_snapshot", "storage", "empty")


async def _snapshot_response(steam_id: str) -> JSONResponse | None:
    """El último inventario bueno, con su fecha, o None si no hay. El cuerpo sigue
    siendo la lista de siempre: el aviso de dato viejo va en cabeceras."""
    try:
        snap = await inventory_snapshot_repo.load(steam_id)
    except StorageError as exc:
        logger.warning("[inventory] no se pudo leer el snapshot: %s", exc)
        log_degraded("inventory_snapshot", "storage", "empty")
        return None
    if snap is None:
        return None
    items, captured_at = snap
    return JSONResponse(items, headers={
        "X-Inventory-Stale": "1",
        "X-Inventory-Captured-At": captured_at,
    })


async def _no_inventory(steam_id: str, reason: str) -> JSONResponse | list:
    """410/411 de steamwebapi (CAL-13): el snapshot con `X-Inventory-Stale` si lo hay,
    si no `[]`. Ni la caché ni el snapshot se pisan con el vacío: un inventario que
    hoy no se puede leer no borra el último que sí se leyó."""
    snap = await _snapshot_response(steam_id)
    log_degraded("inventory", reason, "stale" if snap is not None else "empty")
    return snap if snap is not None else []


def _backoff(attempt: int, retry_after: float | None) -> float:
    """Exponencial con jitter «equal» (mitad fija, mitad aleatoria), y nunca menos
    de lo que pidió steamwebapi en Retry-After."""
    exp = min(INVENTORY_429_BACKOFF_CAP, INVENTORY_429_BACKOFF_BASE * 2 ** attempt)
    return max(retry_after or 0.0, exp / 2 + random.uniform(0, exp / 2))


async def _retry_inventory(request: Request, steam_id: str, retry_after: float | None) -> None:
    # ponytail: tarea en proceso, no cola persistente — si Render duerme se pierde, y
    # el siguiente GET reintenta por su cuenta. Cola (Supabase/Redis) si hace falta.
    try:
        for attempt in range(INVENTORY_429_MAX_RETRIES):
            await asyncio.sleep(_backoff(attempt, retry_after))
            try:
                fetched = await _fetch_fresh_inventory(request, steam_id)
            except RateLimited as exc:
                retry_after = exc.retry_after
                _log_429(steam_id, f"retry-{attempt + 1}", retry_after, "none")
                continue
            except QuotaExhausted:
                logger.warning("[inventory-402] user=%s cuota agotada en el reintento; se aborta", steam_id)
                return
            except HTTPException as exc:   # lo que _fetch_fresh_inventory ya tradujo (403, 502...)
                logger.warning("[inventory] reintento de %s abortado: %s", steam_id, exc.detail)
                return
            if fetched.status == "error":
                logger.warning("[inventory] reintento de %s: %s, no se guarda nada", steam_id, fetched.reason)
                return
            await _store(steam_id, fetched.data, time.monotonic())
            logger.info("[inventory-429] user=%s recuperado en el reintento %d", steam_id, attempt + 1)
            return
        logger.warning("[inventory-429] user=%s sin recuperar tras %d reintentos",
                       steam_id, INVENTORY_429_MAX_RETRIES)
    finally:
        _retry_tasks.pop(steam_id, None)


def _schedule_retry(request: Request, steam_id: str, retry_after: float | None) -> None:
    if steam_id in _retry_tasks:
        return
    # El dict guarda la referencia: asyncio solo tiene referencias débiles a las tareas.
    _retry_tasks[steam_id] = asyncio.create_task(_retry_inventory(request, steam_id, retry_after))


async def _degraded_inventory(request: Request, steam_id: str, exc: Exception, origin: str):
    """429 → snapshot + reintento en segundo plano. 402 → snapshot, SIN reintento."""
    snap = await _snapshot_response(steam_id)
    if isinstance(exc, RateLimited):
        _log_429(steam_id, origin, exc.retry_after, "snapshot" if snap else "none")
        _schedule_retry(request, steam_id, exc.retry_after)
        if snap is None:
            raise HTTPException(status_code=429, detail="Steam rate limit — retry later")
    else:
        logger.warning("[inventory-402] user=%s origin=%s cuota agotada served=%s",
                       steam_id, origin, "snapshot" if snap else "none")
        if snap is None:
            raise HTTPException(status_code=503, detail=UPSTREAM_QUOTA_DETAIL)  # SEC-16
    return snap


@router.get("/inventory", summary="Inventario CS2 del usuario autenticado")
async def get_inventory(request: Request, user: dict = Depends(require_jwt)):
    steam_id: str = user["sub"]

    now = time.monotonic()
    hit = _inventory_cache.fresh(steam_id, now)
    if hit is not None:
        return hit

    # Con un reintento en curso no se vuelve a llamar: cada GET extra sería otro 429.
    if steam_id in _retry_tasks and (snap := await _snapshot_response(steam_id)):
        return snap

    try:
        fetched = await _fetch_fresh_inventory(request, steam_id)
    except (RateLimited, QuotaExhausted) as exc:
        return await _degraded_inventory(request, steam_id, exc, "get")
    if fetched.status == "error":
        return await _no_inventory(steam_id, fetched.reason or "unknown")
    await _store(steam_id, fetched.data, now)
    return fetched.data


@router.post("/inventory/refresh", summary="Fuerza un refresh del inventario ignorando el caché de 23h")
async def refresh_inventory(request: Request, user: dict = Depends(require_jwt)):
    steam_id: str = user["sub"]

    now = time.monotonic()
    if _inventory_refresh_cooldown.fresh(steam_id, now) is not None:
        remaining = int(_inventory_refresh_cooldown.ttl - (now - _inventory_refresh_cooldown[steam_id][1]))
        raise HTTPException(status_code=429, detail=f"Refresh cooldown active — retry in {remaining}s")

    if steam_id in _retry_tasks and (snap := await _snapshot_response(steam_id)):
        return snap

    try:
        fetched = await _fetch_fresh_inventory(request, steam_id)
    except (RateLimited, QuotaExhausted) as exc:
        return await _degraded_inventory(request, steam_id, exc, "refresh")
    if fetched.status == "error":
        return await _no_inventory(steam_id, fetched.reason or "unknown")
    await _store(steam_id, fetched.data, now)
    _inventory_refresh_cooldown.put(steam_id, True, now)
    return fetched.data


@router.get("/item/history", dependencies=[Depends(item_history_rate_limit)], summary="Historial de precios de un item CS2")
async def get_item_history(
    request: Request,
    name: str,
    interval: str = "10",
    market: str | None = None,
    days: int = 35,
    user: dict = Depends(require_jwt),
):
    try:
        fetched = await pricing.get_item_history(
            request.app.state.http_client, name, interval, market, days,
            limiter_timeout=ITEM_HISTORY_LIMITER_TIMEOUT,
        )
    except SOURCE_ERRORS as exc:
        # Ventana llena / 429 sin caché caducada → 503 + Retry-After; 402 → 503 upstream_quota.
        raise http_error_for(exc, timeout_status=504) from exc
    return fetched.data
