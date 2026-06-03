import asyncio
import logging
import time
from datetime import date, timedelta

import httpx

from settings import STEAM_API_KEY
from stores import (
    ITEM_HISTORY_CACHE_TTL, IMAGE_CACHE_TTL, MARKET_LOOKUP_CACHE_TTL,
    MARKET_PROVIDERS_CACHE_TTL,
    _item_history_cache, _item_image_cache, _image_cache_meta,
    _market_lookup_cache, _market_providers_cache,
)
from steam.mappers import _delta_from_history, _map_topmovers_item

logger = logging.getLogger("uvicorn.error")

STEAM_WEB_API = "https://www.steamwebapi.com/steam/api"
STEAM_MARKET_API = "https://www.steamwebapi.com/market"

_STATIC_SKINS_URL     = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json"
_STATIC_STICKERS_URL  = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/stickers.json"
_STATIC_KEYCHAINS_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/keychains.json"
_STATIC_KNIVES_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/knives.json"
_STATIC_CRATES_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/crates.json"
_STATIC_AGENTS_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/agents.json"
_STATIC_PATCHES_URL   = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/patches.json"

_WEAR_NAMES = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]

_MOVERS_LIMIT = 10


# ── Price history ─────────────────────────────────────────────────────────────

async def _fetch_history_for_item(client: httpx.AsyncClient, name: str) -> list:
    cache_key = f"{name}:1d35"
    now = time.monotonic()
    cached = _item_history_cache.get(cache_key)
    if cached and now - cached[1] < ITEM_HISTORY_CACHE_TTL and cached[0]:
        return cached[0]
    try:
        today = date.today()
        resp = await client.get(
            f"{STEAM_WEB_API}/history",
            params={
                "key": STEAM_API_KEY,
                "market_hash_name": name,
                "interval": "1",
                "start_date": (today - timedelta(days=35)).isoformat(),
                "end_date": today.isoformat(),
                "format": "json",
            },
            timeout=30.0,
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


async def _enrich_prices(client: httpx.AsyncClient, items: list, concurrency: int = 5) -> list:
    sem = asyncio.Semaphore(concurrency)

    async def fetch(name: str):
        async with sem:
            return await _fetch_history_for_item(client, name)

    histories = await asyncio.gather(*[fetch(it["name"]) for it in items])
    result = []
    for item, pts in zip(items, histories):
        if pts:
            # Use the price from /items if available — it's more current than history.
            # History may be months old for low-volume items.
            latest = item.get("priceLatest") or pts[-1]["price"]
            item = {
                **item,
                "priceDelta24h": _delta_from_history(pts, 1, latest),
                "priceDelta7d":  _delta_from_history(pts, 7, latest),
                "priceDelta30d": _delta_from_history(pts, 30, latest),
            }
        result.append(item)
    return result


# ── Image cache ───────────────────────────────────────────────────────────────

def _cache_images(raw_items: list) -> None:
    for raw in raw_items:
        img = raw.get("image", "")
        if not img:
            continue
        for key in (raw.get("markethashname"), raw.get("marketname")):
            if key:
                _item_image_cache[key] = img


def _enrich_images_from_cache(items: list) -> None:
    if not _item_image_cache:
        return
    for item in items:
        if not item.get("image"):
            name = item.get("name", "")
            img = _item_image_cache.get(name, "")
            # StatTrak variants share the same skin image as their base version.
            # Removing "StatTrak™ " covers all cases in one step:
            #   "★ StatTrak™ X (wear)" → "★ X (wear)"  (knife, image from API)
            #   "StatTrak™ X (wear)"   → "X (wear)"     (weapon, image from API or ByMykel)
            if not img and "StatTrak™ " in name:
                img = _item_image_cache.get(name.replace("StatTrak™ ", "", 1), "")
            # Non-StatTrak "★ X": ByMykel stores knives without the star prefix.
            if not img and name.startswith("★ "):
                img = _item_image_cache.get(name[2:], "")
            # Souvenir items: try the base skin name without the "Souvenir " prefix.
            if not img and name.startswith("Souvenir "):
                item_type = (item.get("itemType") or "").lower()
                if "charm" not in item_type:
                    img = _item_image_cache.get(name[len("Souvenir "):], "")
            item["image"] = img


def _register_skin(item: dict) -> None:
    name = item.get("name", "")
    image = item.get("image", "")
    if not name or not image:
        return
    wears = [w.get("name", "") for w in item.get("wears", []) if w.get("name")]
    if not wears:
        wears = _WEAR_NAMES
    _item_image_cache[name] = image
    _item_image_cache[f"★ {name}"] = image
    for wear in wears:
        _item_image_cache[f"{name} ({wear})"] = image
        _item_image_cache[f"★ {name} ({wear})"] = image
    if item.get("stattrak"):
        _item_image_cache[f"StatTrak™ {name}"] = image
        _item_image_cache[f"★ StatTrak™ {name}"] = image
        for wear in wears:
            _item_image_cache[f"StatTrak™ {name} ({wear})"] = image
            _item_image_cache[f"★ StatTrak™ {name} ({wear})"] = image


def _register_flat(item: dict) -> None:
    image = item.get("image", "")
    if not image:
        return
    for key_field in ("market_hash_name", "name"):
        key = item.get(key_field, "")
        if key:
            _item_image_cache[key] = image


async def _fetch_static_images(client: httpx.AsyncClient) -> None:
    now = time.monotonic()
    if now - _image_cache_meta.get("ts", 0.0) < IMAGE_CACHE_TTL:
        return

    sources_with_wears = [
        ("skins",  _STATIC_SKINS_URL),
        ("knives", _STATIC_KNIVES_URL),
    ]
    sources_flat = [
        ("stickers",  _STATIC_STICKERS_URL),
        ("keychains", _STATIC_KEYCHAINS_URL),
        ("crates",    _STATIC_CRATES_URL),
        ("agents",    _STATIC_AGENTS_URL),
        ("patches",   _STATIC_PATCHES_URL),
    ]

    total_before = len(_item_image_cache)
    fetched: dict[str, int] = {}

    for label, url in sources_with_wears:
        try:
            resp = await client.get(url, timeout=15.0)
            if resp.status_code != 200:
                logger.warning("[image-cache] %s returned %s", label, resp.status_code)
                continue
            data = resp.json()
            if not isinstance(data, list):
                logger.warning("[image-cache] %s unexpected format: %s", label, type(data).__name__)
                continue
            for item in data:
                _register_skin(item)
            fetched[label] = len(data)
        except Exception as exc:
            logger.warning("[image-cache] could not fetch %s: %s", label, exc)

    for label, url in sources_flat:
        try:
            resp = await client.get(url, timeout=15.0)
            if resp.status_code != 200:
                logger.warning("[image-cache] %s returned %s", label, resp.status_code)
                continue
            data = resp.json()
            if not isinstance(data, list):
                logger.warning("[image-cache] %s unexpected format: %s", label, type(data).__name__)
                continue
            for item in data:
                _register_flat(item)
            fetched[label] = len(data)
        except Exception as exc:
            logger.warning("[image-cache] could not fetch %s: %s", label, exc)

    _image_cache_meta["ts"] = now
    logger.info(
        "[image-cache] loaded %d total entries (%+d new) — sources: %s",
        len(_item_image_cache),
        len(_item_image_cache) - total_before,
        fetched,
    )


# ── Movers ────────────────────────────────────────────────────────────────────

# ── Multi-market price lookup ─────────────────────────────────────────────────

_TRACKED_MARKETS = ("csfloat", "buff")


async def _fetch_market_price_lookup(client: httpx.AsyncClient, market: str) -> dict[str, float]:
    now = time.monotonic()
    cached = _market_lookup_cache.get(market)
    if cached and now - cached[1] < MARKET_LOOKUP_CACHE_TTL:
        return cached[0]
    try:
        resp = await client.get(
            f"{STEAM_MARKET_API}/{market}/prices",
            params={"key": STEAM_API_KEY, "format": "json"},
            timeout=30.0,
        )
        if resp.status_code != 200:
            logger.warning("[market-lookup] %s returned %s", market, resp.status_code)
            return (cached[0] if cached else {})
        data = resp.json()
        if not isinstance(data, list):
            return (cached[0] if cached else {})
        lookup: dict[str, float] = {}
        for item in data:
            name = item.get("market_hash_name") or item.get("markethashname") or item.get("name")
            price = item.get("price") or item.get("value") or 0
            if name and price:
                lookup[name] = float(price)
        _market_lookup_cache[market] = (lookup, now)
        logger.info("[market-lookup] %s: %d prices loaded", market, len(lookup))
        return lookup
    except Exception as exc:
        logger.warning("[market-lookup] could not fetch %s: %s", market, exc)
        return (cached[0] if cached else {})


async def _enrich_market_prices(client: httpx.AsyncClient, items: list) -> list:
    csfloat_lookup, buff_lookup = await asyncio.gather(
        _fetch_market_price_lookup(client, "csfloat"),
        _fetch_market_price_lookup(client, "buff"),
    )
    for item in items:
        name = item.get("name", "")
        item["csfloatPrice"] = csfloat_lookup.get(name) or None
        item["buffPrice"] = buff_lookup.get(name) or None
    return items


_STEAM_FAVICON = "https://store.steampowered.com/favicon.ico"

_PROVIDER_IDS = {"csfloat", "buff"}

# Known public logos used as fallback when the API doesn't return them
_KNOWN_LOGOS: dict[str, str] = {
    "steam":   _STEAM_FAVICON,
    "csfloat": "https://csfloat.com/favicon.ico",
    "buff":    "https://buff.163.com/favicon.ico",
}

_FALLBACK_PROVIDERS = [
    {"id": "steam",   "name": "Steam",   "logoUrl": _KNOWN_LOGOS["steam"]},
    {"id": "csfloat", "name": "CSFloat", "logoUrl": _KNOWN_LOGOS["csfloat"]},
    {"id": "buff",    "name": "Buff163", "logoUrl": _KNOWN_LOGOS["buff"]},
]


async def _fetch_market_providers(client: httpx.AsyncClient) -> list[dict]:
    now = time.monotonic()
    cached = _market_providers_cache.get("providers")
    if cached and now - cached[1] < MARKET_PROVIDERS_CACHE_TTL:
        return cached[0]
    try:
        resp = await client.get(
            f"{STEAM_WEB_API}/info/markets",
            params={"key": STEAM_API_KEY},
            timeout=15.0,
        )
        if resp.status_code != 200:
            logger.warning("[market-providers] info/markets returned %s", resp.status_code)
            return _FALLBACK_PROVIDERS
        data = resp.json()
        if not isinstance(data, list):
            return _FALLBACK_PROVIDERS

        if data:
            logger.info("[market-providers] sample keys: %s", list(data[0].keys()))

        lookup: dict[str, dict] = {}
        for m in data:
            mid = (m.get("id") or m.get("key") or m.get("name") or "").lower()
            if mid in _PROVIDER_IDS:
                api_logo = (
                    m.get("logo") or m.get("logoUrl") or m.get("logo_url") or
                    m.get("image") or m.get("imageUrl") or m.get("image_url") or
                    m.get("icon") or m.get("iconUrl") or m.get("icon_url") or
                    m.get("thumbnail") or ""
                )
                lookup[mid] = {
                    "id":      mid,
                    "name":    m.get("name") or mid.capitalize(),
                    "logoUrl": api_logo or _KNOWN_LOGOS.get(mid, ""),
                }

        providers = [{"id": "steam", "name": "Steam", "logoUrl": _STEAM_FAVICON}]
        for pid in ("csfloat", "buff"):
            providers.append(lookup.get(pid) or next(f for f in _FALLBACK_PROVIDERS if f["id"] == pid))

        _market_providers_cache["providers"] = (providers, now)
        logger.info("[market-providers] loaded %d providers", len(providers))
        return providers
    except Exception as exc:
        logger.warning("[market-providers] failed: %s", exc)
        return _FALLBACK_PROVIDERS


# ── Movers ────────────────────────────────────────────────────────────────────

def _build_movers_from_topmovers(gainers: list, losers: list) -> dict | None:
    if not gainers and not losers:
        return None
    def _is_slab(raw: dict) -> bool:
        name = (raw.get("marketname") or raw.get("markethashname") or "").lower()
        return "sticker slab" in name
    hot  = [_map_topmovers_item(g) for g in gainers if not _is_slab(g)][:_MOVERS_LIMIT]
    cold = [_map_topmovers_item(l) for l in losers  if not _is_slab(l)][:_MOVERS_LIMIT]
    logger.info("[market-movers] topmovers raw: gainers=%d losers=%d | after_filter: hot=%d cold=%d",
                len(gainers), len(losers), len(hot), len(cold))
    hot  = sorted(hot,  key=lambda x: x["_change24h"], reverse=True)
    cold = sorted(cold, key=lambda x: x["_change24h"])
    for item in hot + cold:
        del item["_change24h"]
    return {"hot": hot, "cold": cold}
