import asyncio
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt, StrictStr, StringConstraints, ValidationError

import settings
from steam.cache.user_cache import _inventory_refresh_cooldown
from auth.service import _rate_limit, item_history_rate_limit, require_jwt
from ..domain.models import Fetched
from ..errors.handling import SOURCE_ERRORS, http_error_for
from ..errors import UPSTREAM_QUOTA_DETAIL, UpstreamError
from ..services import inventory_service, own_inventory_service, pricing_service, profile_service
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
    if fetched.status == "error" and fetched.reason == "needs_upload":
        return JSONResponse([], headers={"X-Inventory-Needs-Upload": "1"})
    if fetched.status == "stale":
        headers = {"X-Inventory-Stale": "1", "X-Inventory-Captured-At": fetched.data.captured_at or ""}
        if fetched.reason == "needs_upload":
            headers["X-Inventory-Needs-Upload"] = "1"
        return JSONResponse(fetched.data.items, headers=headers)
    if fetched.reason == "needs_upload":   # `ok` de un refresh en modo client (COST-07b)
        return JSONResponse(fetched.data.items, headers={"X-Inventory-Needs-Upload": "1"})
    return fetched.data.items   # `ok`, o `error` por 410/411: `[]` (CAL-13)


# ── COST-07b: inventario que sube el móvil ───────────────────────────────────
# El cuerpo es ENTRADA NO CONFIABLE: el esquema solo deja pasar los campos que lee
# `worker/decode/inventory.extract`, con tipos y longitudes acotados (extra se descarta).
# Las cotas de las listas importan por la MEMORIA, no por los bytes: cada elemento es un
# objeto de Pydantic, y con 20 000 × 32 elementos vacíos 5 MB de JSON ocupaban ~700 MB
# (revisión del 2026-10-23). Un inventario de CS2 tiene como mucho ~1 000 ítems.
MAX_ITEMS = 5_000
_Id = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,20}$")]   # [0-9]: \d acepta dígitos Unicode
_Num = StrictStr | StrictInt | Annotated[StrictFloat, Field(allow_inf_nan=False)]


class _Asset(BaseModel):
    assetid: _Id
    classid: _Id
    instanceid: _Id
    amount: _Num | None = None


class _Tag(BaseModel):
    category: str | None = Field(None, max_length=64)
    localized_tag_name: str | None = Field(None, max_length=128)
    color: str | None = Field(None, pattern=r"^[0-9A-Fa-f]{0,8}$")


class _Description(BaseModel):
    classid: _Id
    instanceid: _Id
    market_hash_name: str | None = Field(None, max_length=256)
    name: str | None = Field(None, max_length=256)
    icon_url: str | None = Field(None, pattern=r"^[A-Za-z0-9_\-/.]{0,2048}$")
    type: str | None = Field(None, max_length=256)
    tags: list[_Tag] = Field(default_factory=list, max_length=16)
    tradable: StrictInt | StrictBool = 1
    marketable: StrictInt | StrictBool = 1


class _Property(BaseModel):
    propertyid: StrictInt | None = None
    name: str | None = Field(None, max_length=64)
    float_value: _Num | None = None
    int_value: _Num | None = None
    string_value: str | None = Field(None, max_length=1024)   # el certificado va aquí, en hex


class _AssetProperties(BaseModel):
    assetid: _Id
    asset_properties: list[_Property] = Field(default_factory=list, max_length=8)


class InventoryUpload(BaseModel):
    """Páginas de steamcommunity.com/inventory unidas por el móvil (mismas claves que Steam)."""
    assets: list[_Asset] = Field(max_length=MAX_ITEMS)
    descriptions: list[_Description] = Field(max_length=MAX_ITEMS)
    asset_properties: list[_AssetProperties] = Field(default_factory=list, max_length=MAX_ITEMS)


_upload_lock = asyncio.Lock()   # ponytail: global; por usuario no acota la memoria del proceso


def _upload_guard(request: Request, user: Annotated[dict, Depends(require_jwt)]) -> dict:
    """Antes de leer el cuerpo: solo en modo `client`, tamaño declarado y cupo por usuario."""
    if settings.INVENTORY_SOURCE != "client":
        raise HTTPException(status_code=404, detail="Not Found")
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        raise HTTPException(status_code=411, detail="Content-Length required")
    if int(length) > settings.INVENTORY_UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Inventory upload too large")
    _rate_limit(user["sub"], limit=settings.INVENTORY_UPLOAD_PER_MIN, bucket="inventory-upload")
    return user


@router.post("/inventory/upload", summary="Sube el inventario descargado por el móvil (COST-07b)")
async def upload_inventory(request: Request, user: Annotated[dict, Depends(_upload_guard)]):
    # El cuerpo se lee aquí y no como parámetro: FastAPI parsea los parámetros de cuerpo ANTES
    # de las dependencias, y el tope de tamaño de `_upload_guard` llegaría tarde.
    raw = await request.body()
    # Una subida a la vez en todo el proceso (Render: un worker, 512 MB): validar y decodificar
    # es CPU y memoria acotadas por subida, pero no por usuarios a la vez.
    async with _upload_lock:
        try:
            body = await asyncio.to_thread(InventoryUpload.model_validate_json, raw)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_input=False)) from exc
        # exclude_none: `extract` mira `campo in dict`; un None explícito taparía el valor real.
        fetched = await own_inventory_service.ingest_upload(user["sub"], body.model_dump(exclude_none=True))
    return fetched.data.items


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
