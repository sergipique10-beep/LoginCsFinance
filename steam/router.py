import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from settings import STEAM_API_KEY, STEAM_GAME
from stores import (
    PROFILE_CACHE_TTL, INVENTORY_CACHE_TTL, MARKET_INDEX_CACHE_TTL, ITEM_HISTORY_CACHE_TTL,
    _profile_cache, _inventory_cache, _market_index_cache, _item_history_cache,
)
from auth.service import require_jwt, _get_client_ip, _rate_limit
from steam.mappers import (
    _delta_from_history,
    _map_item,
    _map_market_index_point,
    _map_news_item,
    _fetch_og_image,
)

logger = logging.getLogger("uvicorn.error")

STEAM_WEB_API = "https://www.steamwebapi.com/steam/api"


async def _fetch_history_for_item(client: httpx.AsyncClient, name: str) -> list:
    cache_key = f"{name}:10"
    now = time.monotonic()
    cached = _item_history_cache.get(cache_key)
    if cached and now - cached[1] < ITEM_HISTORY_CACHE_TTL:
        return cached[0]
    try:
        resp = await client.get(
            f"{STEAM_WEB_API}/history",
            params={"key": STEAM_API_KEY, "market_hash_name": name, "interval": "10", "format": "json"},
            timeout=8.0,
        )
        if resp.status_code != 200:
            return []
        raw = resp.json()
        if not isinstance(raw, list):
            return []
        pts = sorted(
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
        _item_history_cache[cache_key] = (pts, now)
        return pts
    except Exception:
        return []


async def _enrich_prices(client: httpx.AsyncClient, items: list) -> list:
    histories = await asyncio.gather(*[_fetch_history_for_item(client, it["name"]) for it in items])
    result = []
    for item, pts in zip(items, histories):
        if pts:
            latest = pts[-1]["price"]
            item = {
                **item,
                "priceLatest":   latest,
                "priceDelta24h": _delta_from_history(pts, 1, latest),
                "priceDelta7d":  _delta_from_history(pts, 7, latest),
                "priceDelta30d": _delta_from_history(pts, 30, latest),
            }
        result.append(item)
    return result


router = APIRouter()


@router.get("/me", summary="Info del usuario autenticado")
async def get_me(request: Request, user: dict = Depends(require_jwt)):
    steam_id: str = user["sub"]

    now = time.monotonic()
    cached = _profile_cache.get(steam_id)
    if cached and now - cached[1] < PROFILE_CACHE_TTL:
        return cached[0]

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
    }
    _profile_cache[steam_id] = (profile, now)
    return profile


@router.get("/inventory", summary="Inventario CS2 del usuario autenticado")
async def get_inventory(request: Request, user: dict = Depends(require_jwt)):
    steam_id: str = user["sub"]

    now = time.monotonic()
    cached = _inventory_cache.get(steam_id)
    if cached and now - cached[1] < INVENTORY_CACHE_TTL:
        return cached[0]

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
    items = await _enrich_prices(request.app.state.http_client, items)
    _inventory_cache[steam_id] = (items, now)
    return items


@router.get("/market/index", summary="Índice de mercado global CS2")
async def get_market_index(
    request: Request,
    tf: str = "24h",
    user: dict = Depends(require_jwt),
):
    cache_key = tf
    now = time.monotonic()
    cached = _market_index_cache.get(cache_key)
    if cached and now - cached[1] < MARKET_INDEX_CACHE_TTL:
        return cached[0]

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/market-index/cs2",
            params={"key": STEAM_API_KEY, "format": "json"},
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Market index request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code == 402:
        logger.warning("[market-index] daily limit reached (402)")
        raise HTTPException(status_code=429, detail="Steam API daily limit reached — try again tomorrow")
    if resp.status_code != 200:
        logger.error("[market-index] steamwebapi returned %s | body: %s", resp.status_code, resp.text[:500])
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()

    if isinstance(data, list):
        raw_points = data
        delta_24h = 0.0
        top = None
        turnover24h = 0.0
        sold24h = 0
    elif isinstance(data, dict):
        history = data.get("history", [])
        if isinstance(history, list):
            raw_points = history
        elif isinstance(history, dict):
            raw_points = history.get("priceindex", [])
            if not isinstance(raw_points, list):
                logger.error("[market-index] 'priceindex' unexpected type: %s", type(raw_points).__name__)
                raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")
        else:
            logger.error("[market-index] 'history' unexpected type: %s | sample: %s", type(history).__name__, str(history)[:200])
            raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

        changes_24h = data.get("changes", {}).get("24h", {})
        delta_24h = 0.0
        if isinstance(changes_24h, dict):
            pi_change = changes_24h.get("priceindex", {})
            if isinstance(pi_change, dict):
                delta_24h = float(pi_change.get("change") or 0)

        gainers = data.get("topmovers", {}).get("gainers", [])
        top = gainers[0] if gainers else None
        turnover24h = float(data.get("turnover24h") or 0)
        sold24h = int(data.get("sold24h") or 0)
    else:
        logger.error("[market-index] unexpected top-level type: %s", type(data).__name__)
        raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

    result = {
        "turnover24h": turnover24h,
        "sold24h": sold24h,
        "delta24h": delta_24h,
        "hottestItem": {
            "name": top["markethashname"] if top else "—",
            "change24h": float(top["change24h"]) if top else 0.0,
        },
        "history": [_map_market_index_point(p) for p in raw_points],
    }
    _market_index_cache[cache_key] = (result, now)
    return result


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


@router.get("/news/cs2", summary="Últimas noticias de CS2 vía Steam News API")
async def get_cs2_news(request: Request, count: int = 5):
    _rate_limit(_get_client_ip(request))

    try:
        resp = await request.app.state.http_client.get(
            "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
            params={"appid": 730, "count": count, "format": "json"},
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Steam news request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    newsitems = resp.json().get("appnews", {}).get("newsitems", [])
    images = await asyncio.gather(*[
        _fetch_og_image(request.app.state.http_client, item.get("url", ""))
        for item in newsitems
    ])
    return [_map_news_item(item, i, images[i]) for i, item in enumerate(newsitems)]
