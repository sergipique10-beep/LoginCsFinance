"""Los deltas de precio salen de los campos `pricereal*`, no de `pricelatestsell*`.

steamwebapi devuelve `pricelatestsell24h/7d/30d` *idénticos* a `pricelatestsell`
(comprobado contra la API real: las 5 Desert Eagle | Mulberry devuelven el mismo
número en los cuatro campos). Comparar el precio consigo mismo daba delta=None →
badge "N/A" en todas las skins del inventario. Los campos `pricereal*` sí son
valores históricos reales y coinciden con lo que muestran otros trackers.
"""
from steam.mappers import _map_item
from steam.routes.market import _MOVERS_SELECT


# Respuesta real de steamwebapi /items para esta skin (campos relevantes, 2026-07-14).
DEAGLE_MULBERRY_FN = {
    "markethashname": "Desert Eagle | Mulberry (Factory New)",
    "marketname": "Desert Eagle | Mulberry (Factory New)",
    # Los cuatro planos: la API copia el mismo precio en todos los timeframes.
    "pricelatestsell": 23.719999313354,
    "pricelatestsell24h": 23.719999313354,
    "pricelatestsell7d": 23.719999313354,
    "pricelatestsell30d": 23.719999313354,
    # Los que sí varían.
    "pricereal": 17.57,
    "pricereal24h": 18.0,
    "pricereal7d": 18.71,
    "pricereal30d": 18.35,
}


def test_deltas_se_calculan_desde_pricereal():
    item = _map_item(DEAGLE_MULBERRY_FN)

    assert item["priceDelta24h"] == -2.39   # (17.57 - 18.00) / 18.00
    assert item["priceDelta7d"] == -6.09    # (17.57 - 18.71) / 18.71
    assert item["priceDelta30d"] == -4.25   # (17.57 - 18.35) / 18.35


def test_precio_historico_implausible_se_descarta():
    """La API devuelve ocasionalmente basura (pricereal30d=0.22 con pricereal=17.57).

    Eso daría un +7886% falso. Preferimos None ("N/A") a un delta absurdo.
    """
    item = _map_item({**DEAGLE_MULBERRY_FN, "pricereal30d": 0.22})

    assert item["priceDelta30d"] is None
    assert item["priceDelta7d"] == -6.09  # los demás timeframes no se ven afectados


def test_movers_select_pide_los_campos_que_map_item_necesita():
    """/market/items (search) y /market/trending piden campos concretos con `select`.

    Si `pricereal*` no está en la lista, la API no lo devuelve y _map_item no puede
    calcular ningún delta → "N/A" en todos los resultados de búsqueda del Market.
    """
    campos = set(_MOVERS_SELECT.split(","))

    assert {"pricereal", "pricereal24h", "pricereal7d", "pricereal30d"} <= campos


def test_item_sin_ventas_no_tiene_deltas():
    """Souvenirs sin ventas recientes: la API devuelve None en toda la familia pricereal."""
    item = _map_item({
        "markethashname": "Souvenir Desert Eagle | Mulberry (Factory New)",
        "pricelatest": 97.78,
        "pricereal": None,
        "pricereal24h": None,
        "pricereal7d": None,
        "pricereal30d": None,
    })

    assert item["priceDelta24h"] is None
    assert item["priceDelta7d"] is None
    assert item["priceDelta30d"] is None
