"""El Liquidity Score responde: si listo este ítem hoy, ¿en cuánto se vende y a qué precio real?

Todos los pesos salen de esa pregunta. Un score que midiera "salud del mercado"
tendría otros. Ver docs/superpowers/specs/2026-07-14-liquidity-score-design.md.
"""
from steam.liquidity import compute_liquidity
from steam.mappers import _map_item, _map_topmovers_item
from steam.routes.market import _MOVERS_SELECT


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


def test_item_sin_ventas_puntua_bajo_pero_no_es_none():
    """Cero ventas NO es "sin datos": es "no se mueve". Un None acá sería mentir.

    El único componente que sobrevive es el haircut (bid a $700 de una vitrina de
    $1000 → perdés 30% si salís ya). Los pesos se renormalizan sobre 0.90 porque
    no hay `prices` con dos mercados.
    """
    score, breakdown = compute_liquidity({
        "markethashname": "★ Kukri Knife | Stained (Factory New)",
        "pricelatestsell": 1000.0,
        "buyorderprice": 700.0,
        "sold24h": 0,
        "sold7d": 0,
        "offervolume": 40,
        "buyordervolume": 0,
        "prices": [{"market": "steam", "price": 1000.0, "quantity": 40}],
    })

    assert score == 11.11          # (0.25 × 0.4) / 0.90 × 100
    assert score is not None
    assert breakdown["velocity"]["value"] == 0.0
    assert breakdown["timeToSell"]["value"] == 0.0
    assert breakdown["haircut"]["value"] == 0.4
    assert "consistency" not in breakdown   # un solo mercado → nada que comparar


def test_pesos_se_renormalizan_entre_componentes_disponibles():
    """Sin `prices`, los 4 componentes restantes deben sumar peso 1.0, no 0.90.

    Si no renormalizáramos, todo ítem sin datos de mercados externos tendría un techo
    del 90% — un castigo por un dato que falta, no por ser ilíquido.
    """
    _, breakdown = compute_liquidity({
        "pricelatestsell": 10.0,
        "buyorderprice": 9.0,
        "sold24h": 50,
        "sold7d": 350,
        "offervolume": 100,
        "buyordervolume": 200,
    })

    assert "consistency" not in breakdown
    assert round(sum(c["weight"] for c in breakdown.values()), 4) == 1.0


def test_sin_datos_suficientes_devuelve_none():
    """Menos de la mitad del peso disponible → "N/A", no un 0% que diría "ilíquido"."""
    score, breakdown = compute_liquidity({
        "markethashname": "Souvenir AWP | Dragon Lore (Factory New)",
        "pricelatestsell": 15000.0,
    })

    assert score is None
    assert breakdown is None


def test_bid_por_encima_del_ask_descarta_el_haircut():
    """buyorderprice > pricelatestsell × 1.05 es imposible: basura de la API.

    Sin la guardia, el haircut daría negativo → se recortaría a 1.0 → el ítem
    cobraría 0.25 de peso completo por un dato corrupto.
    """
    basura = {
        "pricelatestsell": 100.0,
        "buyorderprice": 130.0,     # bid 30% por encima del ask
        "sold24h": 10,
        "sold7d": 70,
        "offervolume": 50,
        "buyordervolume": 100,
    }
    sano = {**basura, "buyorderprice": 95.0}

    _, breakdown_basura = compute_liquidity(basura)
    _, breakdown_sano = compute_liquidity(sano)

    assert "haircut" not in breakdown_basura
    assert breakdown_sano["haircut"]["value"] == 0.9   # 1 − (0.05 / 0.50)


def test_map_item_expone_el_score_y_el_desglose():
    item = _map_item(ITEM_LIQUIDO)

    assert item["liquidityScore"] == 67.83
    assert item["liquidityBreakdown"]["velocity"]["value"] == 0.7834


def test_topmovers_no_inventa_un_score():
    """El payload de topmovers no trae offervolume/buyordervolume/hourstosold.

    El mapper los pone en 0 duro. Calcular el score ahí daría un número que diría
    "ilíquido" cuando la verdad es "no hay datos".
    """
    item = _map_topmovers_item({
        "markethashname": "AK-47 | Redline (Field-Tested)",
        "price": 1.50,
        "change24h": 0.03,
    })

    assert item["liquidityScore"] is None
    assert item["liquidityBreakdown"] is None


def test_movers_select_pide_los_campos_del_score():
    """Sin `prices` en el select, el mismo ítem puntúa distinto en Market que en Inventario.

    Los 4 componentes restantes se renormalizarían sobre 0.90 solo en el Market.
    Un score que cambia según la pantalla en la que lo mirás es un bug.
    """
    campos = set(_MOVERS_SELECT.split(","))

    assert {
        "sold24h", "sold7d", "offervolume", "buyordervolume",
        "buyorderprice", "hourstosold", "prices",
    } <= campos
