import asyncio
import html
import re
import time
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import uvicorn

import httpx
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from settings import FRONTEND_URL, STEAM_API_KEY, STEAM_GAME
from middleware import SecurityHeadersMiddleware
from stores import (
    PROFILE_CACHE_TTL, INVENTORY_CACHE_TTL, MARKET_INDEX_CACHE_TTL, ITEM_HISTORY_CACHE_TTL,
    _profile_cache, _inventory_cache, _market_index_cache, _item_history_cache,
)
from auth.router import router as auth_router
from auth.service import require_jwt, _get_client_ip, _rate_limit

logger = logging.getLogger("uvicorn.error")

STEAM_WEB_API = "https://www.steamwebapi.com/steam/api"

# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    if JWT_SECRET == "change-this-secret":
        logger.warning(
            "JWT_SECRET es el valor por defecto inseguro — "
            "define un secreto fuerte en .env"
        )
    if len(JWT_SECRET) < 32:
        logger.warning(
            "JWT_SECRET tiene menos de 32 caracteres — "
            "usa secrets.token_urlsafe(48) para generar un secreto seguro"
        )
    if not STEAM_API_KEY:
        logger.warning(
            "STEAM_API_KEY no está configurada — "
            "los endpoints de Steam Web API no funcionarán"
        )
    app.state.http_client = httpx.AsyncClient(timeout=10.0)
    yield
    await app.state.http_client.aclose()


# ── App + middleware ───────────────────────────────────────────────────────────

app = FastAPI(title="Steam Login", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(auth_router)


# ── Inventory mapper ──────────────────────────────────────────────────────────

def _safe_delta(new: float | None, old: float | None) -> float:
    if not new or not old:
        return 0.0
    return round((new - old) / old * 100, 2)


def _resolve_phase(item: dict) -> str | None:
    paint_index = item.get("paintindex")
    variants = item.get("variants", [])
    if paint_index is None or not variants:
        return None
    match = next((v for v in variants if v.get("paintindex") == paint_index), None)
    return match.get("phase") if match else None


def _map_item(item: dict) -> dict:
    # /float/assets?with_items=1 nests market data under "item"; /inventory is flat
    d = item.get("item") or item

    latest = (
        d.get("pricelatestsell") or
        d.get("price") or
        d.get("lowestprice") or
        d.get("priceusd") or
        0
    )
    p24h = d.get("pricelatestsell24h") or d.get("price24h")
    p7d  = d.get("pricelatestsell7d")  or d.get("price7d")
    p30d = d.get("pricelatestsell30d") or d.get("price30d")

    float_data = item.get("float") or d.get("float") or {}

    return {
        "id":             item.get("assetid") or item.get("id", ""),
        "name":           d.get("marketname", "") or d.get("market_hash_name", ""),
        "slug":           d.get("slug", ""),
        "weaponType":     d.get("weapontype"),
        "itemName":       d.get("itemname"),
        "itemType":       d.get("itemtype"),
        "image":          d.get("image", ""),
        "rarity":         d.get("rarity", "Base Grade"),
        "rarityColor":    d.get("color", "b0c3d9"),
        "borderColor":    d.get("bordercolor", "b0c3d9"),
        "quality":        d.get("quality", "Normal"),
        "isStatTrak":     bool(d.get("isstattrak", False)),
        "isSouvenir":     bool(d.get("issouvenir", False)),
        "isStar":         bool(d.get("isstar", False)),
        "exterior":       d.get("tag5") or d.get("exterior"),
        "floatValue":     float_data.get("floatvalue") if isinstance(float_data, dict) else None,
        "floatMin":       d.get("minfloat"),
        "floatMax":       d.get("maxfloat"),
        "paintIndex":     d.get("paintindex"),
        "phase":          _resolve_phase(d),
        "priceLatest":    latest,
        "priceSafe":      d.get("pricesafe") or 0,
        "priceMin":       d.get("pricemin") or 0,
        "priceMax":       d.get("pricemax") or 0,
        "priceDelta24h":  _safe_delta(latest, p24h),
        "priceDelta7d":   _safe_delta(latest, p7d),
        "priceDelta30d":  _safe_delta(latest, p30d),
        "priceReal":      d.get("pricereal"),
        "externalPrices": [
            {"market": p["market"], "price": p["price"], "quantity": p["quantity"]}
            for p in d.get("prices", [])
        ],
        "sold24h":        d.get("sold24h") or 0,
        "sold7d":         d.get("sold7d") or 0,
        "sold30d":        d.get("sold30d") or 0,
        "soldTotal":      d.get("soldtotal") or 0,
        "offerVolume":    d.get("offervolume") or 0,
        "buyOrderVolume": d.get("buyordervolume") or 0,
        "buyOrderPrice":  d.get("buyorderprice") or 0,
        "hoursToSold":    d.get("hourstosold") or 0,
        "marketable":     bool(d.get("marketable", True)),
        "tradable":       bool(d.get("tradable", True)),
        "tradeLockDays":  d.get("markettradablerestriction"),
        "steamUrl":       d.get("steamurl"),
    }


# ── Rutas ──────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/me", summary="Info del usuario autenticado")
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


@app.get("/inventory", summary="Inventario CS2 del usuario autenticado")
async def get_inventory(
    request: Request,
    user: dict = Depends(require_jwt),
):
    steam_id: str = user["sub"]

    now = time.monotonic()
    cached = _inventory_cache.get(steam_id)
    if cached and now - cached[1] < INVENTORY_CACHE_TTL:
        return cached[0]

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/float/assets",
            params={
                "steam_id": steam_id,
                "game": STEAM_GAME,
                "key": STEAM_API_KEY,
                "language": "english",
                "limit": 5000,
                "with_items": 1,
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
        logger.error("steamwebapi /float/assets → %s: %.500s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()

    if not isinstance(data, list):
        logger.error("steamwebapi /float/assets unexpected format: %.500s", resp.text)
        raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

    if data:
        first = data[0]
        logger.info("[DEBUG inventory] keys del primer item: %s", list(first.keys()))
        logger.info("[DEBUG inventory] float field: %s", first.get("float"))
        logger.info("[DEBUG inventory] minfloat: %s | maxfloat: %s", first.get("minfloat"), first.get("maxfloat"))
        price_fields = {k: v for k, v in first.items() if "price" in k.lower() or "sold" in k.lower() or "delta" in k.lower()}
        logger.info("[DEBUG inventory] price/sold fields: %s", price_fields)

    items = [_map_item(item) for item in data]
    _inventory_cache[steam_id] = (items, now)
    return items


# ── Market index mapper ───────────────────────────────────────────────────────

def _first_not_none(d: dict, *keys):
    """Devuelve el primer valor no-None encontrado entre las keys dadas."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _map_market_index_point(point: dict) -> dict:
    # Cada punto de la serie "priceindex" tiene: ts (unix), value (precio), change (% vs anterior), trend, win
    return {
        "date":   str(point.get("ts", "")),
        "price":  float(point.get("value") or 0),
        "change": float(point.get("change") or 0),
        "volume": int(point.get("volume") or 0),
    }


# ── Market routes ─────────────────────────────────────────────────────────────

@app.get("/market/index", summary="Índice de mercado global CS2")
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
            params={
                "key": STEAM_API_KEY,
                "format": "json",
            },
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
    points = [_map_market_index_point(p) for p in (data if isinstance(data, list) else [])]
    _market_index_cache[cache_key] = (points, now)
    return points


@app.get("/item/history", summary="Historial de precios de un item CS2")
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


async def _fetch_og_image(client: httpx.AsyncClient, url: str) -> str:
    if not url:
        return ""
    try:
        resp = await client.get(
            url, timeout=4.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return ""
        match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)["\']',
            resp.text, re.IGNORECASE,
        ) or re.search(
            r'<meta[^>]+content=["\'](https?://[^"\']+)["\'][^>]+property=["\']og:image["\']',
            resp.text, re.IGNORECASE,
        )
        return match.group(1) if match else ""
    except Exception:
        return ""


def _clean_news_content(raw: str, max_chars: int = 220) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)                    # HTML tags
    text = re.sub(r"\[[^\]]*\]", " ", text)                 # BBCode [b], [url=...], [img] → espacio para no fusionar palabras
    text = re.sub(r"\{[^}]*\}", " ", text)                  # {STEAM_CLAN_IMAGE}, {h2}, {/h2}, etc.
    text = html.unescape(text)                              # &amp; &nbsp; &#39; etc.
    text = re.sub(r"https?://\S+", "", text)                # full URLs (https://...)
    text = re.sub(r"(?<!\w)/\S+", "", text)                 # /path o //cdn tokens
    text = re.sub(r"\s*\\\s*", " ", text)                   # backslash separators (\ Cache, \ Fixed)
    text = " ".join(text.split())                           # normalize whitespace
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0]
    return text


def _map_news_item(item: dict, index: int, image_url: str = "") -> dict:
    feedname  = item.get("feedname", "").lower()
    feedlabel = item.get("feedlabel", "NEWS")

    if "blog" in feedname or "valve" in feedname:
        category_color = "4a9eff"
    elif any(x in feedname for x in ("hltv", "liquipedia", "esport")):
        category_color = "8847ff"
    else:
        category_color = "f0c040"

    try:
        date_str = datetime.fromtimestamp(item["date"], tz=timezone.utc).strftime("%Y-%m-%d")
    except (KeyError, ValueError, OSError):
        date_str = ""

    author = item.get("author", "").strip()

    excerpt = _clean_news_content(item.get("contents", ""))

    return {
        "id":            str(item.get("gid", index)),
        "category":      feedlabel.upper(),
        "categoryColor": category_color,
        "title":         item.get("title", ""),
        "source":        author if author else feedlabel,
        "date":          date_str,
        "imageUrl":      image_url,
        "featured":      index == 0,
        "url":           item.get("url", ""),
        "content":       excerpt,
    }


@app.get("/news/cs2", summary="Últimas noticias de CS2 vía Steam News API")
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


if __name__ == "__main__":
    
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=True)


