"""Mappers puros entre ISkinCard (camelCase) y las tablas de mercado en Supabase.

`_row_to_item` convierte una fila snake_case (p.ej. de market_trending) al shape
ISkinCard usado por el frontend. `_to_row` hace el mapeo inverso, usado tanto por
el trending-tick como (via el parámetro opcional `bucket`) por la futura feature
de movers hot/cold.
"""
from collections.abc import Mapping
from typing import Any

from steam.domain.models import RankedCard, RankingRow


def _row_to_item(row: dict) -> RankedCard:
    """Convierte una fila de market_trending (snake_case) al shape ISkinCard (camelCase).

    OJO — `liquidityBreakdown` NO se emite aquí, y es deliberado: el detail
    sheet del frontend lo usa como señal de "esto es un snapshot pobre, pide el
    item completo a /market/price" (skin-detail-sheet.component.ts). Si algún
    día se añade la columna `liquidity_breakdown` y se devuelve aquí, esa
    heurística deja de dispararse EN SILENCIO y el bloque de liquidez se queda
    vacío para siempre. Lo protege tests/test_steam_contract_rows.py.
    """
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
        "priceReal": row.get("price_real"),
        "sold24h": row.get("sold_24h") or 0,
        "sold7d": row.get("sold_7d") or 0,
        "sold30d": row.get("sold_30d") or 0,
        "soldTotal": 0,
        "offerVolume": row.get("offer_volume") or 0,
        "hoursToSold": row.get("hours_to_sold") or 0,
        "steamUrl": row.get("steam_url"),
    }


def _to_row(item: Mapping[str, Any], rank: int, bucket: str | None = None) -> RankingRow:
    """Convierte un item ISkinCard-shaped (camelCase) a una fila de market_trending (snake_case).

    `bucket` es opcional (usado por movers para distinguir "hot"/"cold"); el
    trending-tick no lo pasa, así que la fila queda igual que antes.
    """
    row: RankingRow = {
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
        "price_real": item.get("priceReal"),
        "sold_24h": int(item.get("sold24h") or 0),
        "sold_7d": int(item.get("sold7d") or 0),
        "sold_30d": int(item.get("sold30d") or 0),
        "offer_volume": int(item.get("offerVolume") or 0),
        "hours_to_sold": item.get("hoursToSold"),
        "steam_url": item.get("steamUrl"),
        # Precalculado para poder ordenar en SQL sin recomputar. Es el mismo
        # criterio que rules.turnover.
        "turnover": (item.get("priceLatest") or 0) * (item.get("sold24h") or 0),
    }
    if bucket is not None:
        row["bucket"] = bucket
    return row

