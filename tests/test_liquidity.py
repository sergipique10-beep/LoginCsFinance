"""El Liquidity Score responde: si listo este ítem hoy, ¿en cuánto se vende y a qué precio real?

Todos los pesos salen de esa pregunta. Un score que midiera "salud del mercado"
tendría otros. Ver docs/superpowers/specs/2026-07-14-liquidity-score-design.md.
"""
from steam.liquidity import compute_liquidity


# Ítem de alta rotación: se vende mucho, hay una montaña de compradores esperando,
# pero los mercados no coinciden en el precio (Steam $1.50 vs CSFloat $0.88).
# Sin `buyorderprice` ni `hourstosold` — el componente de haircut se descarta y el
# tiempo de venta se deriva de la cola de listings.
ITEM_LIQUIDO = {
    "markethashname": "AK-47 | Redline (Field-Tested)",
    "pricelatestsell": 1.50,
    "sold24h": 131,
    "sold7d": 894,
    "offervolume": 185,
    "buyordervolume": 6297,
    "prices": [
        {"market": "steam",   "price": 1.50, "quantity": 185},
        {"market": "buff",    "price": 0.96, "quantity": 40},
        {"market": "csfloat", "price": 0.88, "quantity": 12},
    ],
}


def test_item_liquido_puntua_alto():
    """131 ventas/día y 6297 buy orders: se vende solo.

    Lo que lo frena es el spread del 41% entre Steam y CSFloat (consistencia 0.59)
    y una cola de 185 listings ≈ 34h por delante.
    """
    score, breakdown = compute_liquidity(ITEM_LIQUIDO)

    assert score == 67.83
    assert breakdown["velocity"]["value"] == 0.7834
    assert breakdown["demand"]["value"] == 1.0        # 6297 buy orders satura
    assert breakdown["consistency"]["value"] == 0.5867
    assert "haircut" not in breakdown                 # sin buyorderprice → descartado


def test_componentes_recortados_a_cero_uno():
    """Una case con 5000 ventas/día no puede dar velocity > 1 y romper la escala 0-100."""
    score, breakdown = compute_liquidity({
        **ITEM_LIQUIDO,
        "sold24h": 5000,
        "sold7d": 35000,
        "buyordervolume": 99999,
    })

    assert breakdown["velocity"]["value"] == 1.0
    assert breakdown["demand"]["value"] == 1.0
    assert 0.0 <= score <= 100.0
