"""CAL-09: contrato de los snapshots de ranking (market_trending / market_movers).

Sustituye al self-check `python -m steam.market_rows` (hoy `steam/mappers/rows.py`). `_row_to_item` es lo que sirven
/market/trending y /market/movers, así que su conjunto de claves es contrato con el front.
"""
from steam.mappers.row_mapper import _row_to_item, _to_row

ITEM_KEYS = {
    "borderColor", "buffPrice", "csfloatPrice", "exterior", "floatMax", "floatMin",
    "floatValue", "hoursToSold", "id", "image", "isSouvenir", "isStar", "isStatTrak",
    "itemName", "itemType", "name", "offerVolume", "paintIndex", "phase",
    "priceDelta24h", "priceDelta30d", "priceDelta7d", "priceLatest", "priceMax",
    "priceMin", "priceReal", "priceSafe", "quality", "rarity", "rarityColor", "slug",
    "sold24h", "sold30d", "sold7d", "soldTotal", "steamUrl", "weaponType",
}

SAMPLE = {
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


def test_to_row_bucket_rank_y_turnover():
    row_hot = _to_row(SAMPLE, 2, "hot")
    assert row_hot["bucket"] == "hot"
    assert row_hot["rank"] == 2
    assert row_hot["weapon_type"] == "AK-47"

    row_plain = _to_row(SAMPLE, 0)
    assert "bucket" not in row_plain
    # turnover = precio × volumen 24h, el criterio de orden de /market/trending.
    assert row_plain["turnover"] == 12.5 * 340


def test_ida_y_vuelta_conserva_los_campos():
    item = _row_to_item(_to_row(SAMPLE, 0))
    for key in ("name", "weaponType", "priceDelta7d", "sold24h", "offerVolume",
                "hoursToSold", "priceReal", "steamUrl", "csfloatPrice", "buffPrice"):
        assert item[key] == SAMPLE[key], key


def test_conjunto_exacto_de_claves():
    assert set(_row_to_item(_to_row(SAMPLE, 0))) == ITEM_KEYS
    assert set(_row_to_item({"name": "x"})) == ITEM_KEYS


def test_volumen_ausente_es_cero_no_none():
    # La tarjeta concatena sin guarda ('Vol: ' + item.sold24h): None pintaría "Vol: None/24h".
    assert _row_to_item({"name": "x"})["sold24h"] == 0


def test_no_emite_liquidity_breakdown():
    # NO TOCAR sin leer el docstring de _row_to_item ni CLAUDE.md: el detail sheet
    # detecta los snapshots pobres con `liquidityBreakdown === undefined`. Si la
    # clave empieza a viajar, deja de pedir /market/price sin dar ningún error.
    assert "liquidityBreakdown" not in _row_to_item(_to_row(SAMPLE, 0))
