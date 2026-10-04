"""Mapper de items de steamwebapi (/items, /inventory, /item) a `SkinCard`, y los
deltas de precio. Puro: sin HTTP, sin caché, sin fallback silencioso."""
from datetime import date, timedelta

from steam.domain.catalog import weapon_category
from steam.domain.models import SkinCard
from steam.liquidity import compute_liquidity

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


# Un precio histórico fuera de este rango respecto al actual es basura de la API
# (visto: pricereal30d=0.22 para una skin de 17.57 → +7886%), no un movimiento real.
_MAX_PLAUSIBLE_RATIO = 10.0


def _inline_delta(current: float | None, raw_old) -> float | None:
    """Compute % delta between the current price and a historical one.

    Both must come from the same `pricereal*` family: `pricelatestsell24h/7d/30d`
    are copies of `pricelatestsell`, so any delta derived from them is always None.

    Returns None when either value is missing/zero (no sales data → "N/A") or when
    the historical value is implausibly far from the current one (API garbage).
    """
    new = float(current or 0) or None
    old = float(raw_old or 0) or None
    if not new or not old:
        return None
    if not (1 / _MAX_PLAUSIBLE_RATIO <= old / new <= _MAX_PLAUSIBLE_RATIO):
        return None
    return _safe_delta(new, old)


def _map_item(item: dict) -> SkinCard:
    # /float/assets?with_items=1 nests market data under "item"; /inventory is flat
    d = item.get("item") or item

    latest = (
        d.get("pricelatestsell") or
        d.get("price") or
        d.get("lowestprice") or
        d.get("priceusd") or
        0
    )
    real = d.get("pricereal")
    float_data = item.get("float") or d.get("float") or {}

    # Sobre `d` crudo, no sobre el dict mapeado: abajo `sold24h` y compañía colapsan
    # None a 0, y eso borraría la diferencia entre "no hay datos" y "cero ventas".
    liquidity_score, liquidity_breakdown = compute_liquidity(d)

    return {
        "id":             item.get("assetid") or item.get("id", ""),
        # markethashname es el nombre canónico de Steam, SIEMPRE en inglés e
        # independiente del locale. marketname es el nombre localizado del Market y
        # steamwebapi lo tiene mal guardado para algunos ítems (p.ej. "Solidão
        # (Testada em Campo)" en portugués en vez de "Solitude (Field-Tested)").
        "name":           d.get("markethashname") or d.get("marketname", ""),
        "slug":           d.get("slug", ""),
        "weaponType":     d.get("weapontype") or weapon_category(d.get("itemtype")),
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
        # Deltas contra la familia pricereal, la única cuyos campos por timeframe
        # traen valores históricos de verdad. pricelatestsell24h/7d/30d vienen
        # siempre iguales a pricelatestsell, así que daban None → "N/A" en todo.
        # _enrich_prices puede sobrescribirlos con valores derivados del histórico
        # de csfloat en los endpoints que la llaman (market, trending, movers).
        "priceDelta24h":  _inline_delta(real, d.get("pricereal24h")),
        "priceDelta7d":   _inline_delta(real, d.get("pricereal7d")),
        "priceDelta30d":  _inline_delta(real, d.get("pricereal30d")),
        "priceReal":      real,
        "externalPrices": [
            {"market": p.get("market"), "price": p.get("price"), "quantity": p.get("quantity")}
            for p in d.get("prices", [])
            if p.get("market")
        ],
        "sold24h":        d.get("sold24h") or 0,
        "sold7d":         d.get("sold7d") or 0,
        "sold30d":        d.get("sold30d") or 0,
        "soldTotal":      d.get("soldtotal") or 0,
        "offerVolume":    d.get("offervolume") or 0,
        "buyOrderVolume": d.get("buyordervolume") or 0,
        "buyOrderPrice":  d.get("buyorderprice") or 0,
        "hoursToSold":    d.get("hourstosold") or 0,
        "liquidityScore":     liquidity_score,
        "liquidityBreakdown": liquidity_breakdown,
        "marketable":     bool(d.get("marketable", True)),
        "tradable":       bool(d.get("tradable", True)),
        "tradeLockDays":  d.get("markettradablerestriction"),
        "steamUrl":       d.get("steamurl"),
    }
