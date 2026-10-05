"""Inventario de CS2: descarga compartida por GET /inventory y la tool del chat
`ver_inventario` (`fetch_fresh_inventory`), y la lectura con degradación ante 429/402
(`get_inventory`: snapshot durable + reintento en segundo plano, PERF-14). Sin FastAPI: devuelve `Fetched[Inventory]` y la ruta
decide cabeceras y status.
"""
import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass

import httpx

from settings import INVENTORY_429_BACKOFF_BASE, INVENTORY_429_BACKOFF_CAP, INVENTORY_429_MAX_RETRIES
from steam import inventory_snapshot_repo
from steam.adapters.steam_adapter import adapt_inventory
from steam.api import steam_client
from steam.cache.user_cache import _inventory_cache
from steam.domain.models import Fetched, SkinCard
from steam.errors import QuotaExhausted, RateLimited, StorageError, UpstreamError
from steam.errors.handling import SOURCE_ERRORS, log_degraded, reason_of
from steam.mappers.item_mapper import _map_item
from steam.services import catalog_service, pricing_service

logger = logging.getLogger("uvicorn.error")

# steamwebapi responde 410/411 a un inventario que no puede leer (perfil borrado,
# inventario sin inicializar). No es un fallo nuestro ni un inventario vacío.
_NO_INVENTORY_STATUSES = (410, 411)


@dataclass(frozen=True)
class Inventory:
    """Lo que sirve `get_inventory`: la lista de siempre y, si es un snapshot, su fecha
    (ISO-8601) para la cabecera `X-Inventory-Captured-At`."""
    items: list
    captured_at: str | None = None


async def fetch_fresh_inventory(client: httpx.AsyncClient, steam_id: str, *, track: bool) -> Fetched[list[SkinCard]]:
    """El inventario mapeado y enriquecido (precios CSFloat/Buff e imágenes).

    - 410/411 → `Fetched([], "error", "http_410")`: no hay inventario que servir, pero
      tampoco es un dato. `get_inventory` sirve el snapshot si lo tiene y NO pisa la caché
      ni el snapshot con `[]` (CAL-13: un vacío guardado valdría 23 h y pisaría el snapshot).
    - El resto de errores del cliente suben tal cual. Un cuerpo que no es lista →
      `UnexpectedPayload` (lo lanza el adapter).
    - `track`: registrar los nombres en `tracked_skins` para la captura diaria. Lo hace
      la ruta y no el chat; es la diferencia que había entre las dos descargas.
    """
    try:
        data = await steam_client.inventory(client, steam_id)
    except UpstreamError as exc:
        if exc.status in _NO_INVENTORY_STATUSES:
            return Fetched([], "error", reason_of(exc))
        raise
    items = [_map_item(item) for item in adapt_inventory(data)]   # cuerpo no lista → UnexpectedPayload
    items = await pricing_service.enrich_market_prices(client, items)
    catalog_service.enrich_images_from_cache(items)

    if track:
        # Auto-registro para la captura de precios (best-effort: nunca romper /inventory)
        try:
            from steam.price_history_repo import register_tracked
            names = [i.get("name") for i in items if i.get("name")]
            if names:
                await register_tracked(names, "inventory")
        except StorageError as exc:
            logger.warning("[price] auto-registro de inventario falló: %s", exc)
            log_degraded("tracked_register", "storage", "empty")

    return Fetched(items)


async def _fetch_fresh_inventory(client: httpx.AsyncClient, steam_id: str) -> Fetched[list]:
    """La descarga de la ruta (con registro en tracked_skins). 429 y 402 suben tal cual:
    los degrada `get_inventory` (PERF-14); un 410/411 llega como `Fetched` con
    `status="error"` y lo resuelve `_no_inventory` (CAL-13). El resto de errores los
    traduce la ruta a HTTP."""
    return await fetch_fresh_inventory(client, steam_id, track=True)


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


async def _load_snapshot(steam_id: str) -> Inventory | None:
    """El último inventario bueno, con su fecha, o None si no hay."""
    try:
        snap = await inventory_snapshot_repo.load(steam_id)
    except StorageError as exc:
        logger.warning("[inventory] no se pudo leer el snapshot: %s", exc)
        log_degraded("inventory_snapshot", "storage", "empty")
        return None
    if snap is None:
        return None
    items, captured_at = snap
    return Inventory(items, captured_at)


async def _no_inventory(steam_id: str, reason: str) -> Fetched[Inventory]:
    """410/411 de steamwebapi (CAL-13): el snapshot (`stale`) si lo hay, si no `[]`
    (`error`). Ni la caché ni el snapshot se pisan con el vacío: un inventario que hoy
    no se puede leer no borra el último que sí se leyó."""
    snap = await _load_snapshot(steam_id)
    log_degraded("inventory", reason, "stale" if snap is not None else "empty")
    return Fetched(snap, "stale", reason) if snap is not None else Fetched(Inventory([]), "error", reason)


def _backoff(attempt: int, retry_after: float | None) -> float:
    """Exponencial con jitter «equal» (mitad fija, mitad aleatoria), y nunca menos
    de lo que pidió steamwebapi en Retry-After."""
    exp = min(INVENTORY_429_BACKOFF_CAP, INVENTORY_429_BACKOFF_BASE * 2 ** attempt)
    return max(retry_after or 0.0, exp / 2 + random.uniform(0, exp / 2))


async def _retry_inventory(client: httpx.AsyncClient, steam_id: str, retry_after: float | None) -> None:
    # ponytail: tarea en proceso, no cola persistente — si Render duerme se pierde, y
    # el siguiente GET reintenta por su cuenta. Cola (Supabase/Redis) si hace falta.
    try:
        for attempt in range(INVENTORY_429_MAX_RETRIES):
            await asyncio.sleep(_backoff(attempt, retry_after))
            try:
                fetched = await _fetch_fresh_inventory(client, steam_id)
            except RateLimited as exc:
                retry_after = exc.retry_after
                _log_429(steam_id, f"retry-{attempt + 1}", retry_after, "none")
                continue
            except QuotaExhausted:
                logger.warning("[inventory] user=%s 402 (cuota agotada) en el reintento; se aborta", steam_id)
                return
            except SOURCE_ERRORS as exc:   # lo que la ruta traduciría a HTTP (403, 502...)
                logger.warning("[inventory] reintento de %s abortado: %s", steam_id, reason_of(exc))
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


def _schedule_retry(client: httpx.AsyncClient, steam_id: str, retry_after: float | None) -> None:
    if steam_id in _retry_tasks:
        return
    # El dict guarda la referencia: asyncio solo tiene referencias débiles a las tareas.
    _retry_tasks[steam_id] = asyncio.create_task(_retry_inventory(client, steam_id, retry_after))


async def _degraded_inventory(client: httpx.AsyncClient, steam_id: str,
                              exc: RateLimited | QuotaExhausted, origin: str) -> Fetched[Inventory]:
    """429 → snapshot + reintento en segundo plano. 402 → snapshot, SIN reintento.
    Sin snapshot, `error` con el motivo: la ruta responde 429 o 503 (SEC-16)."""
    snap = await _load_snapshot(steam_id)
    reason = reason_of(exc)
    if isinstance(exc, RateLimited):
        _log_429(steam_id, origin, exc.retry_after, "snapshot" if snap else "none")
        _schedule_retry(client, steam_id, exc.retry_after)
    else:
        logger.warning("[inventory] user=%s origin=%s 402 (cuota agotada) served=%s",
                       steam_id, origin, "snapshot" if snap else "none")
    if snap is None:
        return Fetched(Inventory([]), "error", reason)
    log_degraded("inventory", reason, "stale")
    return Fetched(snap, "stale", reason)


async def get_inventory(client: httpx.AsyncClient, steam_id: str, *,
                        force: bool = False, origin: str = "get") -> Fetched[Inventory]:
    """El inventario para la ruta: caché de 23 h (salvo `force`), snapshot mientras hay
    un reintento en curso, descarga, y degradación ante 429/402/410.

    - `ok`: dato fresco (de caché o recién leído y guardado en caché + snapshot).
    - `stale`: el snapshot; `reason` = `retry_in_progress`, `rate_limit`, `quota` o el
      `http_41x` del inventario ilegible. `data.captured_at` lleva su fecha.
    - `error`: nada que servir; `reason` = `rate_limit` (→ 429), `quota` (→ 503) o
      `http_41x` (→ `[]`).
    403 y el resto de errores de la fuente suben tal cual para que la ruta los traduzca.
    """
    now = time.monotonic()
    if not force:
        hit = _inventory_cache.fresh(steam_id, now)
        if hit is not None:
            return Fetched(Inventory(hit))

    # Con un reintento en curso no se vuelve a llamar: cada GET extra sería otro 429.
    if steam_id in _retry_tasks and (snap := await _load_snapshot(steam_id)):
        return Fetched(snap, "stale", "retry_in_progress")

    try:
        fetched = await _fetch_fresh_inventory(client, steam_id)
    except (RateLimited, QuotaExhausted) as exc:
        return await _degraded_inventory(client, steam_id, exc, origin)
    if fetched.status == "error":
        return await _no_inventory(steam_id, fetched.reason or "unknown")
    await _store(steam_id, fetched.data, now)
    return Fetched(Inventory(fetched.data))
