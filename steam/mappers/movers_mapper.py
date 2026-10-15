"""Mapper de topmovers (`/market-index/cs2`). El ranking hot/cold de respaldo que se
construye con él cuando /items no responde vive en `services/rankings_service.py`."""

from steam.domain.catalog import weapon_category
from steam.domain.models import MoverItem, TopMover
from steam.domain.normalizers import normalize_image_url

# Items por lado (hot y cold) del ranking de movers.
_MOVERS_LIMIT = 10


def _map_topmovers_item(mover: TopMover) -> MoverItem:
    """Un gainer/loser de /market-index/cs2 con el shape de la tarjeta.

    El payload de topmovers trae poco más que {markethashname, price, change24h}.
    change24h es la variación 24 h en PORCENTAJE (UX-35, comprobado contra una
    respuesta real el 2026-10-02), así que es el `priceDelta24h` de la tarjeta. Lo que
    el payload no trae sale como `None` y no como `0` (UX-46): con `0.0` las tarjetas
    del respaldo pintaban «0 %» en vez de «N/A». `_change24h` es la clave de orden
    interna de `_build_movers_from_topmovers`.
    """
    item = mover.item
    return {
        "id":             item.id or item.market_hash_name or "",
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
        "floatValue":     None,
        "floatMin":       item.float_min,
        "floatMax":       item.float_max,
        "paintIndex":     item.paint_index,
        "phase":          None,
        "priceLatest":    item.price,
        "csfloatPrice":   None,
        "buffPrice":      None,
        "priceSafe":      None,
        "priceMin":       None,
        "priceMax":       None,
        "priceDelta24h":  mover.change_24h,
        "priceDelta7d":   None,
        "priceDelta30d":  None,
        "priceReal":      None,
        "externalPrices": [],
        "sold24h":        item.sold_24h,
        "sold7d":         item.sold_7d,
        "sold30d":        item.sold_30d,
        "soldTotal":      item.sold_total,
        "offerVolume":    None,
        "buyOrderVolume": None,
        "buyOrderPrice":  None,
        "hoursToSold":    None,
        # El payload de topmovers no trae offervolume/buyordervolume/hourstosold.
        # Un score calculado sobre ceros diría "ilíquido" en vez de "no hay datos".
        "liquidityScore":     None,
        "liquidityBreakdown": None,
        "marketable":     True,
        "tradable":       True,
        "tradeLockDays":  None,
        "steamUrl":       None,
        "_change24h":     mover.change_24h or 0.0,
    }
