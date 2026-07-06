import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from settings import STEAM_API_KEY, STEAM_GAME
from stores import (
    PROFILE_CACHE_TTL, INVENTORY_CACHE_TTL, ITEM_HISTORY_CACHE_TTL,
    INVENTORY_REFRESH_COOLDOWN,
    _profile_cache, _inventory_cache, _item_history_cache,
    _inventory_refresh_cooldown,
)
from auth.service import require_jwt, _get_client_ip, _rate_limit
from ..mappers import _map_item
from ..services import (
    STEAM_WEB_API,
    _enrich_market_prices,
    _enrich_images_from_cache,
)

logger = logging.getLogger("uvicorn.error")

router = APIRouter()


@router.get("/me", summary="Info del usuario autenticado")
async def get_me(request: Request, user: dict = Depends(require_jwt)):
    steam_id: str = user["sub"]

    now = time.monotonic()
    cached = _profile_cache.get(steam_id)
    if cached and now - cached[1] < PROFILE_CACHE_TTL:
        profile = cached[0]
        return profile if "steam64_id" in profile else {**profile, "steam64_id": steam_id}

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/profile",
            params={"id": steam_id, "key": STEAM_API_KEY},
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Steam profile request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code != 200:
        logger.error("[me] steamwebapi returned %s | body: %s", resp.status_code, resp.text[:300])
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()
    if isinstance(data, list):
        data = data[0] if data else {}

    profile = {
        "userName":       data.get("personaname", ""),
        "avatarUrl":      data.get("avatarfull", ""),
        "avatarThumbUrl": data.get("avatarmedium") or data.get("avatarfull", ""),
        "profileUrl":     data.get("profileurl", ""),
        "isOnline":       data.get("personastate", 0) != 0,
        "steam64_id":     steam_id,
    }
    _profile_cache[steam_id] = (profile, now)
    return profile


async def _fetch_fresh_inventory(request: Request, steam_id: str) -> list:
    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/inventory",
            params={
                "steam_id": steam_id,
                "game": STEAM_GAME,
                "key": STEAM_API_KEY,
                "language": "english",
                "limit": 5000,
            },
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Steam inventory request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="Inventory is private")
    if resp.status_code in (410, 411):
        return []
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Steam rate limit — retry later")
    if resp.status_code != 200:
        logger.error("steamwebapi /inventory → %s: %.500s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()
    if not isinstance(data, list):
        logger.error("steamwebapi /inventory unexpected format: %.500s", resp.text)
        raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

    items = [_map_item(item) for item in data]
    items = await _enrich_market_prices(request.app.state.http_client, items)
    _enrich_images_from_cache(items)
    return items


@router.get("/inventory", summary="Inventario CS2 del usuario autenticado")
async def get_inventory(request: Request, user: dict = Depends(require_jwt)):
    steam_id: str = user["sub"]

    now = time.monotonic()
    cached = _inventory_cache.get(steam_id)
    if cached and now - cached[1] < INVENTORY_CACHE_TTL:
        return cached[0]

    items = await _fetch_fresh_inventory(request, steam_id)
    _inventory_cache[steam_id] = (items, now)
    return items


@router.post("/inventory/refresh", summary="Fuerza un refresh del inventario ignorando el caché de 23h")
async def refresh_inventory(request: Request, user: dict = Depends(require_jwt)):
    steam_id: str = user["sub"]

    now = time.monotonic()
    cooldown_start = _inventory_refresh_cooldown.get(steam_id)
    if cooldown_start and now - cooldown_start < INVENTORY_REFRESH_COOLDOWN:
        remaining = int(INVENTORY_REFRESH_COOLDOWN - (now - cooldown_start))
        raise HTTPException(status_code=429, detail=f"Refresh cooldown active — retry in {remaining}s")

    items = await _fetch_fresh_inventory(request, steam_id)
    _inventory_cache[steam_id] = (items, now)
    _inventory_refresh_cooldown[steam_id] = now
    return items


@router.get("/item/history", summary="Historial de precios de un item CS2")
async def get_item_history(
    request: Request,
    name: str,
    interval: str = "10",
    user: dict = Depends(require_jwt),
):
    _rate_limit(_get_client_ip(request))

    cache_key = f"{name}:{interval}"
    now = time.monotonic()
    cached = _item_history_cache.get(cache_key)
    if cached and now - cached[1] < ITEM_HISTORY_CACHE_TTL:
        return cached[0]

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/history",
            params={"key": STEAM_API_KEY, "market_hash_name": name, "interval": interval, "format": "json"},
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Steam history request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code == 402:
        logger.warning("[item-history] daily limit reached for %s", name)
        return []
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    raw = resp.json() if isinstance(resp.json(), list) else []
    points = sorted(
        [
            {
                "date":   p.get("createdat", "")[:10],
                "price":  float(p.get("price") or 0),
                "volume": int(p.get("sold") or 0),
            }
            for p in raw if p.get("price")
        ],
        key=lambda p: p["date"],
    )
    _item_history_cache[cache_key] = (points, now)
    return points
