"""Mapper de topmovers (`/market-index/cs2`) y el ranking hot/cold de respaldo que se
construye con él cuando /items no responde."""
import logging

from steam.domain.models import MoverItem
from steam.domain.catalog import weapon_category
from steam.domain.names import is_sticker_slab
from steam.mappers.items import _normalize_image

logger = logging.getLogger("uvicorn.error")

# Items por lado (hot y cold) del ranking de movers.
_MOVERS_LIMIT = 10


def _map_topmovers_item(raw: dict) -> MoverItem:
    """Maps a topmovers gainer/loser object from /market-index/cs2 to ISkinCard shape.

    The topmovers payload only contains {markethashname, price, change24h}.
    change24h is the 24h price change as a PERCENTAGE (UX-35, checked against a
    real response on 2026-10-02: a 0.17 $ sticker with change24h=450, losers
    between -52 and -37). This docstring used to say it was an absolute amount.
    The price deltas of the card are still 0.0 (nothing reads them from here);
    _change24h is kept as an internal sort key for _build_movers_from_topmovers.
    """
    latest = float(raw.get("price") or 0)
    change = float(raw.get("change24h") or 0)
    return {
        "id":             raw.get("id", "") or raw.get("markethashname", ""),
        # markethashname siempre en inglés; marketname viene localizado y a veces
        # mal (ver nota en _map_item).
        "name":           raw.get("markethashname") or raw.get("marketname", ""),
        "slug":           raw.get("slug", ""),
        "weaponType":     raw.get("weapontype") or weapon_category(raw.get("itemtype")),
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
        # El payload de topmovers no trae offervolume/buyordervolume/hourstosold.
        # Un score calculado sobre ceros diría "ilíquido" en vez de "no hay datos".
        "liquidityScore":     None,
        "liquidityBreakdown": None,
        "marketable":     True,
        "tradable":       True,
        "tradeLockDays":  None,
        "steamUrl":       None,
        "_change24h":     change,
    }


def _build_movers_from_topmovers(gainers: list, losers: list) -> dict[str, list[MoverItem]] | None:
    if not gainers and not losers:
        return None
    def _is_slab(raw: dict) -> bool:
        return is_sticker_slab(raw.get("marketname") or raw.get("markethashname") or "")
    hot  = [_map_topmovers_item(g) for g in gainers if not _is_slab(g)][:_MOVERS_LIMIT]
    cold = [_map_topmovers_item(l) for l in losers  if not _is_slab(l)][:_MOVERS_LIMIT]
    logger.info("[market-movers] topmovers raw: gainers=%d losers=%d | after_filter: hot=%d cold=%d",
                len(gainers), len(losers), len(hot), len(cold))
    hot  = sorted(hot,  key=lambda x: x["_change24h"], reverse=True)
    cold = sorted(cold, key=lambda x: x["_change24h"])
    for item in hot + cold:
        del item["_change24h"]
    return {"hot": hot, "cold": cold}
