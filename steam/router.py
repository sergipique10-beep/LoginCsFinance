import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from settings import STEAM_API_KEY, STEAM_GAME
from stores import (
    PROFILE_CACHE_TTL, INVENTORY_CACHE_TTL, MARKET_INDEX_CACHE_TTL,
    ITEM_HISTORY_CACHE_TTL, MOVERS_CACHE_TTL, TRENDING_CACHE_TTL, SEARCH_CACHE_TTL,
    _profile_cache, _inventory_cache, _market_index_cache,
    _item_history_cache, _movers_cache, _topmovers_raw_cache, _trending_cache, _search_cache,
)
from auth.service import require_jwt, _get_client_ip, _rate_limit
from steam.mappers import (
    _map_item,
    _map_topmovers_item,
    _map_market_index_point,
    _map_news_item,
    _fetch_og_image,
)
from steam.services import (
    STEAM_WEB_API,
    _MOVERS_LIMIT,
    _fetch_history_for_item,
    _enrich_prices,
    _cache_images,
    _enrich_images_from_cache,
    _fetch_static_images,
    _build_movers_from_topmovers,
)

logger = logging.getLogger("uvicorn.error")

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
    _enrich_images_from_cache(items)
    _inventory_cache[steam_id] = (items, now)
    return items


_MOVERS_SELECT = ",".join([
    "id", "marketname", "markethashname", "slug", "image",
    "pricelatestsell", "pricelatestsell24h",
    "pricelatestsell7d", "pricelatestsell30d",
    "color", "bordercolor", "rarity", "quality",
    "isstattrak", "issouvenir", "isstar",
    "itemtype", "itemname", "tag5",
    "sold24h", "sold7d", "sold30d", "soldtotal",
    "pricesafe", "pricemin", "pricemax",
    "offervolume", "buyordervolume", "buyorderprice",
    "hourstosold", "marketable", "tradable",
    "markettradablerestriction", "steamurl",
    "minfloat", "maxfloat", "paintindex",
])

_TRENDING_LIMIT = 25


@router.get("/market/movers", summary="Top movers del mercado CS2 (hot & cold 24 h)")
async def get_market_movers(request: Request, user: dict = Depends(require_jwt)):
    cache_key = "movers"
    now = time.monotonic()
    cached = _movers_cache.get(cache_key)
    if cached and now - cached[1] < MOVERS_CACHE_TTL:
        return cached[0]

    # ── Primary source: /items (paid plan) ───────────────────────────────────
    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/items",
            params={
                "key": STEAM_API_KEY,
                "game": "cs2",
                "sort_by": "soldZa",
                "max": 200,
                "select": _MOVERS_SELECT,
                "format": "json",
                "production": "1",
            },
            timeout=15.0,
        )
        items_ok = resp.status_code == 200
    except (httpx.TimeoutException, httpx.RequestError):
        items_ok = False
        resp = None

    if items_ok and resp is not None:
        data = resp.json()
        if isinstance(data, list):
            _cache_images(data)
            mapped = []
            for raw in data:
                latest = float(raw.get("pricelatestsell") or 0)
                volume = int(raw.get("sold24h") or 0)
                if latest > 0 and volume >= 5 and "sticker slab" not in (raw.get("marketname") or "").lower():
                    mapped.append(_map_item(raw))
            # Sort by price descending — higher-priced items tend to have more price movement.
            # Cap at 30 to avoid exhausting /history rate limits on first load.
            # _fetch_history_for_item caches results for 23h so subsequent calls are free.
            mapped.sort(key=lambda x: x["priceLatest"], reverse=True)
            candidates = mapped[:30]
            logger.info("[market-movers] candidates before enrich: %d (capped from %d)", len(candidates), len(mapped))
            candidates = await _enrich_prices(request.app.state.http_client, candidates)
            by_delta = sorted(candidates, key=lambda x: (x["priceDelta7d"] is None, x["priceDelta7d"] or 0))
            logger.info("[market-movers] enriched=%d cold_candidates=%s",
                        len(candidates),
                        [x["name"] for x in by_delta[:_MOVERS_LIMIT]])
            result = {
                "hot":  list(reversed(by_delta[-_MOVERS_LIMIT:])),
                "cold": by_delta[:_MOVERS_LIMIT],
            }
            _enrich_images_from_cache(result["hot"])
            _enrich_images_from_cache(result["cold"])
            _movers_cache[cache_key] = (result, now)
            return result
        logger.warning("[market-movers] /items returned unexpected type: %s", type(data).__name__)
    else:
        if resp is not None:
            logger.warning("[market-movers] /items returned %s — falling back to market-index topmovers", resp.status_code)

    # ── Fallback: market-index topmovers (free plan) ─────────────────────────
    raw_topmovers = _topmovers_raw_cache.get("latest")
    if not raw_topmovers:
        # topmovers cache is cold — fetch market-index now to populate it
        try:
            mi_resp = await request.app.state.http_client.get(
                f"{STEAM_WEB_API}/market-index/cs2",
                params={"key": STEAM_API_KEY, "format": "json"},
                timeout=15.0,
            )
            if mi_resp.status_code == 200:
                mi_data = mi_resp.json()
                if isinstance(mi_data, dict):
                    tm = mi_data.get("topmovers", {})
                    gainers = tm.get("gainers", [])
                    losers  = tm.get("losers", [])
                    if gainers:
                        logger.info("[market-movers] topmovers gainer keys: %s", list(gainers[0].keys()))
                        logger.info("[market-movers] topmovers gainer sample: %s", gainers[0])
                    _topmovers_raw_cache["latest"] = (gainers, losers, now)
                    raw_topmovers = _topmovers_raw_cache["latest"]
        except Exception as exc:
            logger.warning("[market-movers] could not fetch market-index for topmovers: %s", exc)

    if raw_topmovers:
        gainers, losers, _ = raw_topmovers
        result = _build_movers_from_topmovers(gainers, losers)
        if result:
            await _fetch_static_images(request.app.state.http_client)
            _enrich_images_from_cache(result["hot"])
            _enrich_images_from_cache(result["cold"])
            logger.info("[market-movers] serving from market-index topmovers (%d hot, %d cold)", len(result["hot"]), len(result["cold"]))
            _movers_cache[cache_key] = (result, now)
            return result

    # ── Stale cache as last resort ────────────────────────────────────────────
    stale = _movers_cache.get(cache_key)
    if stale:
        logger.info("[market-movers] serving stale cache (%.0f s old)", now - stale[1])
        return stale[0]

    logger.warning("[market-movers] no data available from any source")
    return {"hot": [], "cold": []}


_SEARCH_LIMIT = 30


@router.get("/market/items", summary="Busca items en el mercado CS2 por nombre")
async def get_market_items(
    request: Request,
    q: str,
    user: dict = Depends(require_jwt),
):
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="q is required")

    cache_key = query.lower()
    now = time.monotonic()
    cached = _search_cache.get(cache_key)
    if cached and now - cached[1] < SEARCH_CACHE_TTL:
        return cached[0]

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/items",
            params={
                "key": STEAM_API_KEY,
                "game": "cs2",
                "name": query,
                "max": _SEARCH_LIMIT,
                "select": _MOVERS_SELECT,
                "format": "json",
                "production": "1",
            },
            timeout=15.0,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code == 402:
        raise HTTPException(status_code=429, detail="Steam API daily limit reached — try again tomorrow")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()
    if not isinstance(data, list):
        raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

    _cache_images(data)
    result = [
        _map_item(raw) for raw in data
        if float(raw.get("pricelatestsell") or 0) > 0
        and "sticker slab" not in (raw.get("marketname") or raw.get("market_hash_name") or "").lower()
    ][:_SEARCH_LIMIT]

    _search_cache[cache_key] = (result, now)
    logger.info("[market-items] q=%r → %d results", query, len(result))
    return result


@router.get("/market/trending", summary="Items trending del mercado CS2 (por volumen 24h)")
async def get_market_trending(request: Request, user: dict = Depends(require_jwt)):
    cache_key = "trending"
    now = time.monotonic()
    cached = _trending_cache.get(cache_key)
    if cached and now - cached[1] < TRENDING_CACHE_TTL:
        return cached[0]

    # ── Primary source: /items (paid plan) ───────────────────────────────────
    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/items",
            params={
                "key": STEAM_API_KEY,
                "game": "cs2",
                "sort_by": "soldZa",
                "max": 60,
                "select": _MOVERS_SELECT,
                "format": "json",
                "production": "1",
            },
            timeout=15.0,
        )
        items_ok = resp.status_code == 200
    except (httpx.TimeoutException, httpx.RequestError):
        items_ok = False
        resp = None

    if items_ok and resp is not None:
        data = resp.json()
        if isinstance(data, list):
            _cache_images(data)
            result = []
            for raw in data:
                latest = float(raw.get("pricelatestsell") or 0)
                volume = int(raw.get("sold24h") or 0)
                if latest > 0 and volume >= 1:
                    result.append(_map_item(raw))
            result = sorted(result, key=lambda x: x["sold24h"], reverse=True)[:_TRENDING_LIMIT]
            result = await _enrich_prices(request.app.state.http_client, result)
            _enrich_images_from_cache(result)
            _trending_cache[cache_key] = (result, now)
            return result
        logger.warning("[market-trending] /items returned unexpected type: %s", type(data).__name__)
    else:
        if resp is not None:
            logger.warning("[market-trending] /items returned %s — falling back to topmovers", resp.status_code)

    # ── Fallback: topmovers from cache (free plan) ────────────────────────────
    raw_topmovers = _topmovers_raw_cache.get("latest")
    if not raw_topmovers:
        try:
            mi_resp = await request.app.state.http_client.get(
                f"{STEAM_WEB_API}/market-index/cs2",
                params={"key": STEAM_API_KEY, "format": "json"},
                timeout=15.0,
            )
            if mi_resp.status_code == 200:
                mi_data = mi_resp.json()
                if isinstance(mi_data, dict):
                    tm = mi_data.get("topmovers", {})
                    gainers = tm.get("gainers", [])
                    losers  = tm.get("losers", [])
                    _topmovers_raw_cache["latest"] = (gainers, losers, now)
                    raw_topmovers = _topmovers_raw_cache["latest"]
        except Exception as exc:
            logger.warning("[market-trending] could not fetch market-index for topmovers: %s", exc)

    if raw_topmovers:
        gainers, losers, _ = raw_topmovers
        combined = gainers + losers
        if combined:
            await _fetch_static_images(request.app.state.http_client)
            result = [_map_topmovers_item(item) for item in combined]
            _enrich_images_from_cache(result)
            result = sorted(result, key=lambda x: x["sold24h"], reverse=True)[:_TRENDING_LIMIT]
            _trending_cache[cache_key] = (result, now)
            logger.info("[market-trending] serving from topmovers (%d items)", len(result))
            return result

    # ── Stale cache as last resort ────────────────────────────────────────────
    stale = _trending_cache.get(cache_key)
    if stale:
        logger.info("[market-trending] serving stale cache (%.0f s old)", now - stale[1])
        return stale[0]

    logger.warning("[market-trending] no data available from any source")
    return []


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

        topmovers = data.get("topmovers", {})
        gainers = topmovers.get("gainers", [])
        losers  = topmovers.get("losers", [])
        top = gainers[0] if gainers else None
        if gainers:
            logger.info("[market-index] topmovers gainer keys: %s", list(gainers[0].keys()))
            logger.info("[market-index] topmovers gainer sample: %s", gainers[0])
        _topmovers_raw_cache["latest"] = (gainers, losers, now)
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
