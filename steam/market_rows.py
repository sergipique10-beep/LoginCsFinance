"""Mappers puros entre ISkinCard (camelCase) y las tablas de mercado en Supabase.

`_row_to_item` convierte una fila snake_case (p.ej. de market_trending) al shape
ISkinCard usado por el frontend. `_to_row` hace el mapeo inverso, usado tanto por
el trending-tick como (via el parámetro opcional `bucket`) por la futura feature
de movers hot/cold.
"""


def _row_to_item(row: dict) -> dict:
    """Convierte una fila de market_trending (snake_case) al shape ISkinCard (camelCase).

    OJO — `liquidityBreakdown` NO se emite aquí, y es deliberado: el detail
    sheet del frontend lo usa como señal de "esto es un snapshot pobre, pide el
    item completo a /market/price" (skin-detail-sheet.component.ts). Si algún
    día se añade la columna `liquidity_breakdown` y se devuelve aquí, esa
    heurística deja de dispararse EN SILENCIO y el bloque de liquidez se queda
    vacío para siempre. El self-check de abajo lo protege.
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


def _to_row(item: dict, rank: int, bucket: str | None = None) -> dict:
    """Convierte un item ISkinCard-shaped (camelCase) a una fila de market_trending (snake_case).

    `bucket` es opcional (usado por movers para distinguir "hot"/"cold"); el
    trending-tick no lo pasa, así que la fila queda igual que antes.
    """
    row = {
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
        # criterio que _turnover en routes/market.py.
        "turnover": (item.get("priceLatest") or 0) * (item.get("sold24h") or 0),
    }
    if bucket is not None:
        row["bucket"] = bucket
    return row


if __name__ == "__main__":
    sample_item = {
        "name": "AK-47 | Redline (Field-Tested)",
        "slug": "ak-47-redline",
        "weaponType": "AK-47",
        "itemName": "Redline",
        "itemType": "Rifle",
        "image": "https://example.com/ak.png",
        "rarity": "Classified",
        "rarityColor": "d32ce6",
        "borderColor": "d32ce6",
        "quality": "Normal",
        "isStatTrak": False,
        "isSouvenir": False,
        "isStar": False,
        "exterior": "Field-Tested",
        "floatMin": 0.15,
        "floatMax": 0.38,
        "paintIndex": 282,
        "phase": None,
        "priceLatest": 12.5,
        "csfloatPrice": 12.3,
        "buffPrice": 11.9,
        "priceDelta24h": 1.2,
        "priceDelta7d": -3.4,
        "priceDelta30d": 5.6,
        "priceReal": 12.1,
        "sold24h": 340,
        "sold7d": 2100,
        "sold30d": 9000,
        "offerVolume": 812,
        "hoursToSold": 1.7,
        "steamUrl": "https://steamcommunity.com/market/listings/730/AK-47",
    }

    row_hot = _to_row(sample_item, 2, "hot")
    assert row_hot["bucket"] == "hot"
    assert row_hot["rank"] == 2
    assert row_hot["weapon_type"] == "AK-47"

    row_plain = _to_row(sample_item, 0)
    assert "bucket" not in row_plain

    # turnover = precio × volumen 24h, el criterio de orden de /market/trending.
    assert row_plain["turnover"] == 12.5 * 340

    round_tripped = _row_to_item(_to_row(sample_item, 0))
    assert round_tripped["name"] == sample_item["name"]
    assert round_tripped["weaponType"] == sample_item["weaponType"]
    assert round_tripped["priceDelta7d"] == sample_item["priceDelta7d"]
    assert round_tripped["sold24h"] == sample_item["sold24h"]
    assert round_tripped["offerVolume"] == sample_item["offerVolume"]

    # Volumen ausente → 0, no None: la tarjeta concatena el valor sin guarda
    # ('Vol: ' + item.sold24h) y un None pintaría "Vol: None/24h".
    assert _row_to_item({"name": "x"})["sold24h"] == 0

    # NO TOCAR sin leer el docstring de _row_to_item: el detail sheet detecta
    # los snapshots pobres con `liquidityBreakdown === undefined`. Si esta clave
    # empieza a viajar, el sheet deja de pedir /market/price sin dar ningún
    # error y el bloque de liquidez se queda vacío para siempre.
    assert "liquidityBreakdown" not in round_tripped

    print("OK: market_rows self-check passed")
