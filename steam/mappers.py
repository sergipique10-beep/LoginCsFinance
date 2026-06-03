import asyncio
import html
import re
from datetime import date, datetime, timedelta, timezone

import httpx

_STEAM_CDN = "https://community.akamai.steamstatic.com"


def _normalize_image(raw: str) -> str:
    """Normalize steamwebapi image values to a full Steam CDN URL.

    /items and /inventory return a full URL (community.akamai.steamstatic.com) — pass through.
    Defensive branches handle edge cases (relative path, bare hash) that could appear
    in less-documented endpoints like topmovers from /market-index.
    Empty string is returned as-is so the template @if(imageUrl()) shows no broken image.
    """
    if not raw:
        return ""
    if raw.startswith("http"):
        return raw
    if raw.startswith("/economy/image/"):
        return _STEAM_CDN + raw
    # bare hash — defensive, not observed in /items but possible in other endpoints
    return f"{_STEAM_CDN}/economy/image/{raw}"


# ── Inventory mappers ─────────────────────────────────────────────────────────

def _delta_from_history(pts: list, days: int, latest: float) -> float | None:
    """Returns the % change between `latest` and the price `days` ago in `pts`.

    Returns None if there are no data points within the requested window —
    callers should treat None as 'no recent sales data' rather than 0% change.
    """
    if not pts or not latest:
        return None
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    past = [p for p in pts if p["date"] <= cutoff]
    if not past:
        return None
    ref = past[-1]["price"]
    if not ref:
        return None
    return round((latest - ref) / ref * 100, 2)


def _best_price_from_markets(prices: list) -> float:
    """Returns the best available price from the external prices array (Steam market preferred)."""
    if not prices:
        return 0.0
    for p in prices:
        if "steam" in str(p.get("market") or "").lower():
            v = float(p.get("price") or p.get("value") or 0)
            if v:
                return v
    return float(prices[0].get("price") or prices[0].get("value") or 0)


def _safe_delta(new: float | None, old: float | None) -> float | None:
    if not new or not old:
        return None
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
        "image":          _normalize_image(d.get("image", "")),
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
        "csfloatPrice":   None,
        "buffPrice":      None,
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


# ── Market index topmovers mapper ────────────────────────────────────────────

def _map_topmovers_item(raw: dict) -> dict:
    """Maps a topmovers gainer/loser object from /market-index/cs2 to ISkinCard shape.

    The topmovers payload only contains {markethashname, price, change24h}.
    change24h is an absolute price value, not a percentage — it cannot be used
    as a delta. All price deltas are set to 0.0; _change24h is kept as an
    internal sort key for _build_movers_from_topmovers.
    """
    latest = float(raw.get("price") or 0)
    change = float(raw.get("change24h") or 0)
    return {
        "id":             raw.get("id", "") or raw.get("markethashname", ""),
        "name":           raw.get("marketname", "") or raw.get("markethashname", ""),
        "slug":           raw.get("slug", ""),
        "weaponType":     raw.get("weapontype") or raw.get("itemtype"),
        "itemName":       raw.get("itemname"),
        "itemType":       raw.get("itemtype"),
        "image":          _normalize_image(raw.get("image", "")),
        "rarity":         raw.get("rarity", "Base Grade"),
        "rarityColor":    raw.get("color", "b0c3d9"),
        "borderColor":    raw.get("bordercolor", "b0c3d9"),
        "quality":        raw.get("quality", "Normal"),
        "isStatTrak":     bool(raw.get("isstattrak", False)),
        "isSouvenir":     bool(raw.get("issouvenir", False)),
        "isStar":         bool(raw.get("isstar", False)),
        "exterior":       raw.get("tag5") or raw.get("exterior"),
        "floatValue":     None,
        "floatMin":       raw.get("minfloat"),
        "floatMax":       raw.get("maxfloat"),
        "paintIndex":     raw.get("paintindex"),
        "phase":          None,
        "priceLatest":    latest,
        "csfloatPrice":   None,
        "buffPrice":      None,
        "priceSafe":      0,
        "priceMin":       0,
        "priceMax":       0,
        "priceDelta24h":  0.0,
        "priceDelta7d":   0.0,
        "priceDelta30d":  0.0,
        "priceReal":      None,
        "externalPrices": [],
        "sold24h":        int(raw.get("sold24h") or 0),
        "sold7d":         int(raw.get("sold7d") or 0),
        "sold30d":        int(raw.get("sold30d") or 0),
        "soldTotal":      int(raw.get("soldtotal") or 0),
        "offerVolume":    0,
        "buyOrderVolume": 0,
        "buyOrderPrice":  0,
        "hoursToSold":    0,
        "marketable":     True,
        "tradable":       True,
        "tradeLockDays":  None,
        "steamUrl":       None,
        "_change24h":     change,
    }


# ── Market index mappers ──────────────────────────────────────────────────────

def _first_not_none(d: dict, *keys):
    """Returns the first non-None value found among the given keys."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _map_market_index_point(point: dict) -> dict:
    return {
        "date":   str(point.get("ts", "")),
        "price":  float(point.get("value") or 0),
        "change": float(point.get("change") or 0),
        "volume": int(point.get("volume") or 0),
    }


# ── News mappers ──────────────────────────────────────────────────────────────

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
    text = re.sub(r"<[^>]+>", " ", raw)           # HTML tags
    text = re.sub(r"\[[^\]]*\]", " ", text)        # BBCode [b], [url=...], [img]
    text = re.sub(r"\{[^}]*\}", " ", text)         # {STEAM_CLAN_IMAGE}, {h2}, etc.
    text = html.unescape(text)                     # &amp; &nbsp; &#39; etc.
    text = re.sub(r"https?://\S+", "", text)       # full URLs
    text = re.sub(r"(?<!\w)/\S+", "", text)        # /path or //cdn tokens
    text = re.sub(r"\s*\\\s*", " ", text)          # backslash separators
    text = " ".join(text.split())
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
