"""Mapper de items de steamwebapi (/items, /inventory, /item) a `SkinCard`, y los
deltas de precio. Puro: sin HTTP, sin caché, sin fallback silencioso."""
from datetime import date, timedelta

from steam.domain.catalog import weapon_category
from steam.domain.liquidity import compute_liquidity
from steam.domain.models import SkinCard, SteamItem
from steam.domain.normalizers import normalize_image_url
from steam.domain.rules import plausible_ratio


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


def _resolve_phase(item: SteamItem) -> str | None:
    if item.paint_index is None or not item.variants:
        return None
    match = next((v for v in item.variants if v.paint_index == item.paint_index), None)
    return match.phase if match else None


def _inline_delta(current: float | None, raw_old: float | None) -> float | None:
    """Compute % delta between the current price and a historical one.

    Both must come from the same `pricereal*` family: `pricelatestsell24h/7d/30d`
    are copies of `pricelatestsell`, so any delta derived from them is always None.

    Returns None when either value is missing/zero (no sales data → "N/A") or when
    the historical value is implausibly far from the current one (API garbage).
    """
    new = current or None
    old = raw_old or None
    if not new or not old:
        return None
    if not plausible_ratio(new, old):
        return None
    return _safe_delta(new, old)


def _map_item(item: SteamItem) -> SkinCard:
    """`SteamItem` → tarjeta. La tarjeta es presentación y contrato con el front: los
    volúmenes ausentes salen como `0` y los flags ausentes como su valor por defecto
    (`tests/test_steam_contract_rows.py`). La distinción None/0 vive en `SteamItem`,
    que es lo que recibe `compute_liquidity`."""
    liquidity_score, liquidity_breakdown = compute_liquidity(item)
    real = item.price_real

    return {
        "id":             item.asset_id or item.id or "",
        # markethashname es el nombre canónico de Steam, SIEMPRE en inglés e
        # independiente del locale. marketname es el nombre localizado del Market y
        # steamwebapi lo tiene mal guardado para algunos ítems (p.ej. "Solidão
        # (Testada em Campo)" en portugués en vez de "Solitude (Field-Tested)").
        "name":           item.name,
        "slug":           item.slug or "",
        "weaponType":     item.weapon_type or weapon_category(item.item_type),
        "itemName":       item.item_name,
        "itemType":       item.item_type,
        "image":          normalize_image_url(item.image),
        "rarity":         item.rarity if item.rarity is not None else "Base Grade",
        "rarityColor":    item.color if item.color is not None else "b0c3d9",
        "borderColor":    item.border_color if item.border_color is not None else "b0c3d9",
        "quality":        item.quality if item.quality is not None else "Normal",
        "isStatTrak":     bool(item.is_stattrak),
        "isSouvenir":     bool(item.is_souvenir),
        "isStar":         bool(item.is_star),
        "exterior":       item.exterior,
        "floatValue":     item.float_value,
        "floatMin":       item.float_min,
        "floatMax":       item.float_max,
        "paintIndex":     item.paint_index,
        "phase":          _resolve_phase(item),
        "priceLatest":    item.latest_price or 0,
        "csfloatPrice":   None,
        "buffPrice":      None,
        "priceSafe":      item.price_safe or 0,
        "priceMin":       item.price_min or 0,
        "priceMax":       item.price_max or 0,
        # Deltas contra la familia pricereal, la única cuyos campos por timeframe
        # traen valores históricos de verdad. pricelatestsell24h/7d/30d vienen
        # siempre iguales a pricelatestsell, así que daban None → "N/A" en todo.
        # pricing.enrich_prices puede sobrescribirlos con valores derivados del histórico
        # de csfloat en los endpoints que la llaman (market, trending, movers).
        "priceDelta24h":  _inline_delta(real, item.price_real_24h),
        "priceDelta7d":   _inline_delta(real, item.price_real_7d),
        "priceDelta30d":  _inline_delta(real, item.price_real_30d),
        "priceReal":      real,
        "externalPrices": [
            {"market": q.market, "price": q.price, "quantity": q.quantity}
            for q in item.prices if q.market
        ],
        "sold24h":        item.sold_24h or 0,
        "sold7d":         item.sold_7d or 0,
        "sold30d":        item.sold_30d or 0,
        "soldTotal":      item.sold_total or 0,
        "offerVolume":    item.offer_volume or 0,
        "buyOrderVolume": item.buy_order_volume or 0,
        "buyOrderPrice":  item.buy_order_price or 0,
        "hoursToSold":    item.hours_to_sold or 0,
        "liquidityScore":     liquidity_score,
        "liquidityBreakdown": liquidity_breakdown,
        "marketable":     True if item.marketable is None else item.marketable,
        "tradable":       True if item.tradable is None else item.tradable,
        "tradeLockDays":  item.trade_lock_days,
        "steamUrl":       item.steam_url,
    }
