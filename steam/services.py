import asyncio
import logging
import time
from datetime import date, timedelta

import httpx

from settings import STEAM_API_KEY
from stores import (
    ITEM_HISTORY_CACHE_TTL, IMAGE_CACHE_TTL,
    _item_history_cache, _item_image_cache, _image_cache_meta,
)
from steam.mappers import _delta_from_history, _map_topmovers_item

logger = logging.getLogger("uvicorn.error")

STEAM_WEB_API = "https://www.steamwebapi.com/steam/api"

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
