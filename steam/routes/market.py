import logging
import secrets
import time
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from settings import STEAM_API_KEY, CAP_TICK_TOKEN
from stores import (
    MARKET_INDEX_CACHE_TTL, MOVERS_CACHE_TTL,
    SEARCH_CACHE_TTL, MARKET_PRICES_CACHE_TTL,
    _market_index_cache, _movers_cache, _topmovers_raw_cache,
    _search_cache, _market_prices_cache,
)
from auth.service import require_jwt
from ..cap_history_repo import insert_snapshot, fetch_range
from ..trending_repo import replace_snapshot, fetch_snapshot
from ..mappers import _map_item, _map_topmovers_item, _map_market_index_point, _category_rank
from ..services import (
    STEAM_WEB_API,
    STEAM_MARKET_API,
    _MOVERS_LIMIT,
    _enrich_prices,
    _enrich_market_prices,
    _fetch_market_providers,
    _cache_images,
    _enrich_images_from_cache,
    _fetch_static_images,
    _build_movers_from_topmovers,
)

logger = logging.getLogger("uvicorn.error")

router = APIRouter()

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

# Capped to fit one 60s rate-limit window: _enrich_prices fires one csfloat/history
# call per item, throttled to 18/60s by _history_limiter. A higher limit would make
# the first (cold-cache) request block for minutes waiting on the limiter.
_TRENDING_LIMIT = 18
_SEARCH_LIMIT = 30

_VALID_MARKETS = frozenset({
    "buff", "skinport", "skinbaron", "dmarket", "waxpeer",
    "bitskins", "csgotm", "haloskins", "tradeit", "skinbid",
    "csfloat", "youpin",
})


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
            # Cap at 20 (= _MOVERS_LIMIT * 2): exactly the number of items displayed,
            # and safe within the Starter plan's 20 req/min rate limit.
            mapped.sort(key=lambda x: x["priceLatest"], reverse=True)
            candidates = mapped[:_MOVERS_LIMIT * 2]
            logger.info("[market-movers] candidates: %d (capped from %d)", len(candidates), len(mapped))
            candidates = await _enrich_prices(request.app.state.http_client, candidates)
            with_delta = sorted(
                [x for x in candidates if x["priceDelta7d"] is not None],
                key=lambda x: x["priceDelta7d"],
            )
            no_delta = [x for x in candidates if x["priceDelta7d"] is None]
            logger.info("[market-movers] enriched=%d with_delta=%d no_delta=%d",
                        len(candidates), len(with_delta), len(no_delta))
            # hot = highest positive deltas; cold = lowest/most-negative deltas.
            # Fill remaining slots with no-delta items only if needed.
            hot  = list(reversed(with_delta[-_MOVERS_LIMIT:])) + no_delta
            cold = with_delta[:_MOVERS_LIMIT] + no_delta
            result = {
                "hot":  hot[:_MOVERS_LIMIT],
                "cold": cold[:_MOVERS_LIMIT],
            }
            # steamwebapi /items no devuelve `image` en este plan → el cache estático
            # (ByMykel) es la única fuente. Igual que en /market/items y /market/trending.
            await _fetch_static_images(request.app.state.http_client)
            _enrich_images_from_cache(result["hot"])
            _enrich_images_from_cache(result["cold"])
            await _enrich_market_prices(request.app.state.http_client, result["hot"])
            await _enrich_market_prices(request.app.state.http_client, result["cold"])
            has_deltas = any(
                item.get("priceDelta7d") is not None
                for item in result["hot"] + result["cold"]
            )
            if has_deltas:
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
            await _enrich_market_prices(request.app.state.http_client, result["hot"])
            await _enrich_market_prices(request.app.state.http_client, result["cold"])
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
                "search": query,
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

    await _fetch_static_images(request.app.state.http_client)
    result = await _enrich_market_prices(request.app.state.http_client, result)
    _enrich_images_from_cache(result)

    _search_cache[cache_key] = (result, now)
    logger.info("[market-items] q=%r → %d results", query, len(result))
    return result


async def _compute_trending(client: httpx.AsyncClient) -> list[dict]:
    """Calcula el ranking trending actual (sin cache, sin persistencia).

    Llamado tanto por GET /market/trending (antes de la migración a
    Supabase) como por POST /internal/trending-tick.
    """
    now = time.monotonic()

    # ── Primary source: /items (paid plan) ───────────────────────────────────
    try:
        resp = await client.get(
            f"{STEAM_WEB_API}/items",
            params={
                "key": STEAM_API_KEY,
                "game": "cs2",
                "sort_by": "soldZa",
                "max": 150,
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
            result = sorted(
                result,
                key=lambda x: (_category_rank(x.get("weaponType")), -(x.get("sold24h") or 0)),
            )[:_TRENDING_LIMIT]
            result = await _enrich_prices(client, result)
            result = await _enrich_market_prices(client, result)
            # steamwebapi /items no devuelve `image` en este plan → el cache estático
            # (ByMykel) es la única fuente. Igual que en /market/items (search).
            await _fetch_static_images(client)
            _enrich_images_from_cache(result)
            return result
        logger.warning("[market-trending] /items returned unexpected type: %s", type(data).__name__)
    else:
        if resp is not None:
            logger.warning("[market-trending] /items returned %s — falling back to topmovers", resp.status_code)

    # ── Fallback: topmovers from cache (free plan) ────────────────────────────
    raw_topmovers = _topmovers_raw_cache.get("latest")
    if not raw_topmovers:
        try:
            mi_resp = await client.get(
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
            await _fetch_static_images(client)
            result = [_map_topmovers_item(item) for item in combined]
            _enrich_images_from_cache(result)
            result = sorted(result, key=lambda x: x["sold24h"], reverse=True)[:_TRENDING_LIMIT]
            result = await _enrich_market_prices(client, result)
            logger.info("[market-trending] serving from topmovers (%d items)", len(result))
            return result

    logger.warning("[market-trending] no data available from any source")
    return []


def _row_to_item(row: dict) -> dict:
    """Convierte una fila de market_trending (snake_case) al shape ISkinCard (camelCase)."""
    return {
        "id": row["name"],
        "name": row["name"],
        "slug": row.get("slug", ""),
        "weaponType": row.get("weapon_type"),
        "itemName": row.get("item_name"),
        "itemType": row.get("item_type"),
        "image": row.get("image", ""),
        "rarity": row.get("rarity", "Base Grade"),
        "rarityColor": row.get("rarity_color", "b0c3d9"),
        "borderColor": row.get("border_color", "b0c3d9"),
        "quality": row.get("quality", "Normal"),
        "isStatTrak": row.get("is_stat_trak", False),
        "isSouvenir": row.get("is_souvenir", False),
        "isStar": row.get("is_star", False),
        "exterior": row.get("exterior"),
        "floatValue": None,
        "floatMin": row.get("float_min"),
        "floatMax": row.get("float_max"),
        "paintIndex": row.get("paint_index"),
        "phase": row.get("phase"),
        "priceLatest": row.get("price_latest", 0),
        "csfloatPrice": row.get("csfloat_price"),
        "buffPrice": row.get("buff_price"),
        "priceSafe": 0,
        "priceMin": 0,
        "priceMax": 0,
        "priceDelta24h": row.get("price_delta_24h"),
        "priceDelta7d": row.get("price_delta_7d"),
        "priceDelta30d": row.get("price_delta_30d"),
    }


@router.get("/market/trending", summary="Items trending del mercado CS2 (por volumen 24h)")
async def get_market_trending(request: Request, user: dict = Depends(require_jwt)):
    rows = await fetch_snapshot()
    return [_row_to_item(row) for row in rows]


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


_CAP_TF_MAP: dict[str, timedelta] = {
    "7d":  timedelta(days=7),
    "1m":  timedelta(days=30),
    "3m":  timedelta(days=90),
    "6m":  timedelta(days=180),
    "1y":  timedelta(days=365),
    "3y":  timedelta(days=1095),
}

# Tamaño de bucket de downsampling por timeframe. A 3 años de snapshots
# horarios serían ~26k puntos crudos; agrupando se mantiene el payload acotado.
_CAP_BUCKET_MAP: dict[str, timedelta] = {
    "7d":  timedelta(hours=1),
    "1m":  timedelta(hours=6),
    "3m":  timedelta(days=1),
    "6m":  timedelta(days=1),
    "1y":  timedelta(weeks=1),
    "3y":  timedelta(weeks=1),
}

_CAP_FIELDS = ("priceindex", "realpriceindex", "buyorderpriceindex", "turnover24h")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_ts(ts: str) -> datetime:
    """Parsea un timestamptz ISO (con 'Z' o offset) a datetime aware en UTC."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _downsample(rows: list[dict], bucket: timedelta) -> list[dict]:
    """
    Agrupa filas por floor(ts / bucket) y promedia cada campo.
    Mantiene { ts, v } (v = priceindex) y añade real/buyorder/turnover medios.
    `ts` de salida = inicio del bucket. Asume `rows` ordenado por ts asc.
    """
    bucket_s = bucket.total_seconds()
    grouped: dict[float, list[dict]] = {}
    order: list[float] = []
    for row in rows:
        ts = _parse_ts(row["ts"])
        idx = (ts - _EPOCH).total_seconds() // bucket_s
        if idx not in grouped:
            grouped[idx] = []
            order.append(idx)
        grouped[idx].append(row)

    out: list[dict] = []
    for idx in order:
        members = grouped[idx]
        start = _EPOCH + timedelta(seconds=idx * bucket_s)
        point: dict = {"ts": start.isoformat().replace("+00:00", "Z")}
        for field in _CAP_FIELDS:
            vals = [m[field] for m in members if m.get(field) is not None]
            point[field] = sum(vals) / len(vals) if vals else None
        # Contrato con el frontend: v = priceindex.
        point["v"] = point["priceindex"]
        out.append(point)
    return out


@router.post("/internal/cap-tick", summary="Captura un snapshot del índice de precio CS2 (cron interno)")
async def cap_tick(
    request: Request,
    x_cap_token: str | None = Header(default=None),
):
    if not CAP_TICK_TOKEN or not x_cap_token or not secrets.compare_digest(x_cap_token, CAP_TICK_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing cap-tick token")

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/market-index/cs2",
            params={"key": STEAM_API_KEY, "format": "json"},
            timeout=15.0,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code != 200:
        logger.warning("[cap-tick] market-index returned %s", resp.status_code)
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

    price_index = data.get("priceindex")
    if price_index is None:
        logger.warning("[cap-tick] 'priceindex' missing from response")
        raise HTTPException(status_code=502, detail="'priceindex' missing from Steam response")

    def _num(value):
        return float(value) if value is not None else None

    # Floor al inicio de la hora: la PK es `ts`, así que varias capturas dentro
    # de la misma hora colapsan en una sola fila (upsert idempotente).
    hour_ts = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    point = {
        "ts": hour_ts.isoformat().replace("+00:00", "Z"),
        "priceindex": float(price_index or 0),
        "realpriceindex": _num(data.get("realpriceindex")),
        "buyorderpriceindex": _num(data.get("buyorderpriceindex")),
        "turnover24h": _num(data.get("turnover24h")),
    }

    await insert_snapshot(point)
    logger.info("[cap-tick] snapshot saved: %s = %.4f", point["ts"], point["priceindex"])
    return {"ok": True, "ts": point["ts"], "priceindex": point["priceindex"]}


@router.get("/market/cap-history", summary="Historial del índice de precio CS2 (snapshots horarios)")
async def get_market_cap_history(
    tf: str = "7d",
    user: dict = Depends(require_jwt),
):
    if tf not in _CAP_TF_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tf '{tf}'. Valid values: {', '.join(_CAP_TF_MAP)}",
        )
    cutoff = datetime.now(timezone.utc) - _CAP_TF_MAP[tf]
    rows = await fetch_range(cutoff)
    return _downsample(rows, _CAP_BUCKET_MAP[tf])


def _to_row(item: dict, rank: int) -> dict:
    """Convierte un item ISkinCard-shaped (camelCase) a una fila de market_trending (snake_case)."""
    return {
        "name": item["name"],
        "rank": rank,
        "slug": item.get("slug", ""),
        "weapon_type": item.get("weaponType"),
        "item_name": item.get("itemName"),
        "item_type": item.get("itemType"),
        "image": item.get("image", ""),
        "rarity": item.get("rarity", "Base Grade"),
        "rarity_color": item.get("rarityColor", "b0c3d9"),
        "border_color": item.get("borderColor", "b0c3d9"),
        "quality": item.get("quality", "Normal"),
        "is_stat_trak": bool(item.get("isStatTrak", False)),
        "is_souvenir": bool(item.get("isSouvenir", False)),
        "is_star": bool(item.get("isStar", False)),
        "exterior": item.get("exterior"),
        "float_min": item.get("floatMin"),
        "float_max": item.get("floatMax"),
        "paint_index": item.get("paintIndex"),
        "phase": item.get("phase"),
        "price_latest": item.get("priceLatest", 0),
        "csfloat_price": item.get("csfloatPrice"),
        "buff_price": item.get("buffPrice"),
        "price_delta_24h": item.get("priceDelta24h"),
        "price_delta_7d": item.get("priceDelta7d"),
        "price_delta_30d": item.get("priceDelta30d"),
    }


@router.post("/internal/trending-tick", summary="Captura el ranking trending del mercado CS2 (cron interno)")
async def trending_tick(
    request: Request,
    x_cap_token: str | None = Header(default=None),
):
    if not CAP_TICK_TOKEN or not x_cap_token or not secrets.compare_digest(x_cap_token, CAP_TICK_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing cap-tick token")

    items = await _compute_trending(request.app.state.http_client)
    rows = [_to_row(item, rank) for rank, item in enumerate(items)]
    await replace_snapshot(rows)
    logger.info("[trending-tick] snapshot saved: %d items", len(rows))
    return {"ok": True, "count": len(rows)}


@router.get("/market/providers", summary="Lista de markets soportados como price providers")
async def get_market_providers(request: Request, user: dict = Depends(require_jwt)):
    providers = await _fetch_market_providers(request.app.state.http_client)
    return providers


@router.get("/market/prices", summary="Precios en tiempo real de un item por mercado")
async def get_market_prices(
    request: Request,
    market: str,
    name: str | None = None,
    currency: str | None = None,
    user: dict = Depends(require_jwt),
):
    market = market.lower().strip()
    if market not in _VALID_MARKETS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown market '{market}'. Valid: {', '.join(sorted(_VALID_MARKETS))}",
        )

    cache_key = f"{market}:{(name or '').lower()}:{(currency or 'usd').lower()}"
    now = time.monotonic()
    cached = _market_prices_cache.get(cache_key)
    if cached and now - cached[1] < MARKET_PRICES_CACHE_TTL:
        return cached[0]

    params: dict = {"key": STEAM_API_KEY}
    if name:
        params["market_hash_name"] = name
    if currency:
        params["currency"] = currency

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_MARKET_API}/{market}/prices",
            params=params,
            timeout=15.0,
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Market prices request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code == 402:
        raise HTTPException(status_code=429, detail="Steam API daily limit reached — try again tomorrow")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Market '{market}' not found or no prices available")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()
    _market_prices_cache[cache_key] = (data, now)
    logger.info("[market-prices] market=%r name=%r → %s items", market, name, len(data) if isinstance(data, list) else "object")
    return data
