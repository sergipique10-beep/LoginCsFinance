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
from stores import (
    INVENTORY_REFRESH_COOLDOWN,
    _inventory_cache,
    _inventory_refresh_cooldown,
)
from auth.service import item_history_rate_limit, require_jwt
from .. import inventory_snapshot_repo
from ..degraded import log_degraded, reason_of
from ..errors import (
    UPSTREAM_QUOTA_DETAIL, UPSTREAM_RATE_LIMIT_DETAIL, HistoryBusy, QuotaExhausted, RateLimited,
    SourceTimeout, SourceUnavailable, UnexpectedPayload, UpstreamError,
)
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
        return await profile_service.get_profile(request.app.state.http_client, steam_id)
    except SourceTimeout:
        raise HTTPException(status_code=504, detail="Steam profile request timed out") from None
    except SourceUnavailable as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}") from exc
    except UpstreamError as exc:
        logger.error("[me] steamwebapi returned %s | body: %s", exc.status, exc.body_excerpt[:300])
        raise HTTPException(status_code=502, detail=f"Steam returned {exc.status}") from exc


async def _fetch_fresh_inventory(request: Request, steam_id: str) -> list:
    """El inventario recién leído, con los errores de steamwebapi traducidos a HTTP.
    429 y 402 suben tal cual: los degrada quien llama (PERF-14)."""
    try:
        return await inventory_service.fetch_fresh_inventory(
            request.app.state.http_client, steam_id, track=True,
        )
    except SourceTimeout:
        raise HTTPException(status_code=504, detail="Steam inventory request timed out") from None
    except SourceUnavailable as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}") from exc
    except (QuotaExhausted, RateLimited):
        raise
    except UpstreamError as exc:
        if exc.status == 403:
            raise HTTPException(status_code=403, detail="Inventory is private") from exc
        if exc.status in (410, 411):
            # CAL-13: se sirve y se guarda como inventario vacío (CLEAN-12: con su línea).
            log_degraded("inventory", reason_of(exc), "empty")
            return []
        logger.error("steamwebapi /inventory → %s: %.500s", exc.status, exc.body_excerpt)
        raise HTTPException(status_code=502, detail=f"Steam returned {exc.status}") from exc
    except UnexpectedPayload as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
    except Exception as exc:  # noqa: BLE001 — best-effort: nunca romper /inventory
        logger.warning("[inventory] no se pudo guardar el snapshot: %s", exc)


async def _snapshot_response(steam_id: str) -> JSONResponse | None:
    """El último inventario bueno, con su fecha, o None si no hay. El cuerpo sigue
    siendo la lista de siempre: el aviso de dato viejo va en cabeceras."""
    try:
        snap = await inventory_snapshot_repo.load(steam_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[inventory] no se pudo leer el snapshot: %s", exc)
        return None
    if snap is None:
        return None
    items, captured_at = snap
    return JSONResponse(items, headers={
        "X-Inventory-Stale": "1",
        "X-Inventory-Captured-At": captured_at,
    })


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
                items = await _fetch_fresh_inventory(request, steam_id)
            except RateLimited as exc:
                retry_after = exc.retry_after
                _log_429(steam_id, f"retry-{attempt + 1}", retry_after, "none")
                continue
            except QuotaExhausted:
                logger.warning("[inventory-402] user=%s cuota agotada en el reintento; se aborta", steam_id)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("[inventory] reintento de %s abortado: %r", steam_id, exc)
                return
            await _store(steam_id, items, time.monotonic())
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
        items = await _fetch_fresh_inventory(request, steam_id)
    except (RateLimited, QuotaExhausted) as exc:
        return await _degraded_inventory(request, steam_id, exc, "get")
    await _store(steam_id, items, now)
    return items


@router.post("/inventory/refresh", summary="Fuerza un refresh del inventario ignorando el caché de 23h")
async def refresh_inventory(request: Request, user: dict = Depends(require_jwt)):
    steam_id: str = user["sub"]

    now = time.monotonic()
    cooldown_start = _inventory_refresh_cooldown.get(steam_id)
    if cooldown_start and now - cooldown_start < INVENTORY_REFRESH_COOLDOWN:
        remaining = int(INVENTORY_REFRESH_COOLDOWN - (now - cooldown_start))
        raise HTTPException(status_code=429, detail=f"Refresh cooldown active — retry in {remaining}s")

    if steam_id in _retry_tasks and (snap := await _snapshot_response(steam_id)):
        return snap

    try:
        items = await _fetch_fresh_inventory(request, steam_id)
    except (RateLimited, QuotaExhausted) as exc:
        return await _degraded_inventory(request, steam_id, exc, "refresh")
    await _store(steam_id, items, now)
    _inventory_refresh_cooldown[steam_id] = now
    return items


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
    except SourceTimeout:
        raise HTTPException(status_code=504, detail="Steam history request timed out") from None
    except SourceUnavailable as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}") from exc
    except (HistoryBusy, RateLimited):
        # Ventana de steamwebapi llena y sin caché caducada que servir.
        raise HTTPException(status_code=503, detail=UPSTREAM_RATE_LIMIT_DETAIL,
                            headers={"Retry-After": "60"}) from None
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"Steam returned {exc.status}") from exc
    return fetched.data
