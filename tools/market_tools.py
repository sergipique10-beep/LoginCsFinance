"""Tools de mercado para el orquestador de Sharky.

Cada tool es un wrapper ligero sobre la lógica existente en
``steam/services/``. No duplica lógica —
importa y llama funciones existentes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from steam.errors import HistoryBusy

# PERF-03: el _history_limiter (18 req/60 s) hace esperar a los crons, que es lo
# correcto para batch; en el chat esa espera va dentro de la respuesta al usuario
# (medido: el mismo "hola" entre 2,5 y 10,9 s). Tope solo en el camino del chat.
CHAT_LIMITER_TIMEOUT = 3.0
HISTORY_BUSY_MSG = (
    "El histórico de precios está saturado ahora mismo. "
    "Responde con los datos que tengas y dilo."
)

from tools.registry import register_tool

logger = logging.getLogger("uvicorn.error")

# Campos que el modelo necesita para razonar sobre un item. El resto (image,
# slug, colores, ids, floats...) son de presentación: los pinta el frontend
# desde sus propios endpoints, no salen de aquí.
_CAMPOS_LLM = (
    "name", "priceLatest", "priceDelta24h", "priceDelta7d", "priceDelta30d",
    "sold24h", "liquidityScore", "rarity", "itemType",
)

# Tope de items por tool de listado. El payload se acumula en `contents` vuelta
# a vuelta del loop de tools, y Gemini devuelve 503 por encima de ~20 KB
# (medido: 7 KB → 200 en 3.6s; 20 KB → 503 tras 115s). Con 8 items proyectados
# una tool de listado ocupa ~1 KB, así que tres vueltas caben de sobra.
_TOP_ITEMS_LLM = 8

# Tick mínimo del mercado de Steam: los precios se mueven de centavo en centavo.
_TICK_USD = 0.01
# Suelo de precio (USD; ~10 EUR) para que un item llegue al modelo. Alineado con
# MIN_RANKING_PRICE de steam/domain/validators.py: los rankings ya se capturan
# filtrados, esto es la segunda barrera para lo que venga de otras fuentes
# (búsqueda, inventario) y para los snapshots capturados antes del cambio.
_PRECIO_MIN_LLM = 10.80
# Cuántos ticks debe superar el movimiento para considerarse señal. Con 1, un
# item de $0.10 necesita >9.7% (su propio tick) y uno de $6 pasa con un 0.17%.
# Subirlo a 2 dejaba fuera movimientos legítimos de items de precio medio: una
# AK Ice Coaled a $6 con +0.25% es señal real y no llegaba al 0.33% exigido.
_TICKS_MIN_SENAL = 1


def _tiene_senal(item: Mapping[str, Any]) -> bool:
    """¿El movimiento de este item significa algo, o es ruido de granularidad?

    Una Galil a $0.10 que "sube un 11%" ha subido un centavo — el tick mínimo.
    Ese movimiento no se puede capturar: el spread y las comisiones (~15%) se lo
    comen varias veces. Presentarlo junto a un movimiento real de una AK de $28
    invita a leer como tendencia lo que solo es la resolución del mercado.
    """
    precio = item.get("priceLatest") or 0
    if precio < _PRECIO_MIN_LLM:
        return False

    delta = item.get("priceDelta24h")
    if delta is None:  # sin datos de movimiento: que decida el modelo con el resto
        return True

    umbral_pct = _TICKS_MIN_SENAL * 100 * _TICK_USD / precio
    return abs(delta) > umbral_pct


def _para_llm(items: Sequence[Mapping[str, Any]], limite: int = _TOP_ITEMS_LLM) -> list[dict]:
    """Filtra el ruido, proyecta a los campos que el modelo usa y recorta.

    Sin la proyección, `ver_movers` mete 20 items × 29 campos (17 KB) en el
    contexto y ahí se quedan para todas las vueltas siguientes del loop.

    Sin el filtro, el modelo recibe items de céntimos ordenados por volumen y no
    tiene alternativa que ofrecer: describe fielmente lo que le llega. El sesgo
    está en la fuente, no en el prompt.
    """
    con_senal = [it for it in items if _tiene_senal(it)]
    # Si el filtro deja la lista vacía, es mejor devolver los items crudos que
    # hacer creer al modelo que no hay mercado: que lo explique él con los datos.
    elegidos = con_senal or items
    return [
        {k: it[k] for k in _CAMPOS_LLM if it.get(k) is not None}
        for it in elegidos[:limite]
    ]


# ── consultar_precio_skin ─────────────────────────────────────────────────────

async def _consultar_precio_skin(*, market_hash_name: str, client: httpx.AsyncClient) -> dict:
    """Devuelve precio detallado de una skin por nombre exacto."""
    from stores import _item_price_cache
    from steam.adapters.steam_adapter import adapt_items
    from steam.mappers.item_mapper import _map_item
    from steam.services import catalog, pricing
    from steam.services.market import search_items

    import time

    query = market_hash_name.strip()
    cache_key = query.lower()
    now = time.monotonic()

    # Cache de precio individual
    hit = _item_price_cache.fresh(cache_key, now)
    if hit is not None:
        return hit

    client_http: httpx.AsyncClient = client
    data = await search_items(
        client_http,
        query,
        max=30,
        select="id,marketname,markethashname,slug,image,pricelatestsell,pricereal,pricereal24h,pricereal7d,pricereal30d,color,bordercolor,rarity,quality,isstattrak,issouvenir,isstar,itemtype,itemname,tag5,sold24h,sold7d,sold30d,soldtotal,pricesafe,pricemin,pricemax,offervolume,buyordervolume,buyorderprice,prices,hourstosold,marketable,tradable,markettradablerestriction,steamurl,minfloat,maxfloat,paintindex",
    )
    if not isinstance(data, list):
        return {"error": "formato inesperado de Steam API"}

    raw = next((i for i in adapt_items(data) if i.name.lower() == cache_key), None)
    if raw is None:
        return {"error": f"skin '{query}' no encontrada"}

    catalog.cache_images([raw])
    # dict y no SkinCard: el chat le añade `aviso`, que no es del contrato con el front.
    item: dict[str, Any] = dict(_map_item(raw))
    try:
        (item,) = await pricing.enrich_prices(client_http, [item], limiter_timeout=CHAT_LIMITER_TIMEOUT)
        enriched = True
    except HistoryBusy:
        # PERF-03: mejor un precio sin deltas en 3 s que uno completo en 60.
        enriched = False
        item["aviso"] = HISTORY_BUSY_MSG
    (item,) = await pricing.enrich_market_prices(client_http, [item])
    await catalog.fetch_static_images(client_http)
    catalog.enrich_images_from_cache([item])

    if enriched:
        _item_price_cache.put(cache_key, item, now)
    return item


# ── buscar_skin ───────────────────────────────────────────────────────────────

async def _buscar_skin(*, query: str, client: httpx.AsyncClient) -> list[dict]:
    """Busca skins por nombre y devuelve resultados relevantes."""
    from stores import _search_cache
    from steam.domain.names import is_sticker_slab
    from steam.adapters.steam_adapter import adapt_items
    from steam.mappers.item_mapper import _map_item
    from steam.services import catalog, pricing
    from steam.services.market import search_items

    import time

    q = query.strip()
    if not q:
        return []

    cache_key = q.lower()
    now = time.monotonic()
    hit = _search_cache.fresh(cache_key, now)
    if hit is not None:
        return _para_llm(hit)

    data = await search_items(
        client,
        q,
        max=10,
        select="id,marketname,markethashname,slug,image,pricelatestsell,pricereal,pricereal24h,pricereal7d,pricereal30d,color,bordercolor,rarity,quality,isstattrak,issouvenir,isstar,itemtype,itemname,tag5,sold24h",
    )
    if not isinstance(data, list):
        return []

    items = adapt_items(data)
    catalog.cache_images(items)
    result = [
        _map_item(item) for item in items
        if (item.price_latest_sell or 0) > 0
        and not is_sticker_slab(item.market_name or item.market_hash_name or "")
    ][:10]

    await catalog.fetch_static_images(client)
    result = await pricing.enrich_market_prices(client, result)
    catalog.enrich_images_from_cache(result)

    # El cache guarda el item completo (lo consumen otros callers); la proyección
    # es solo para lo que ve el modelo.
    _search_cache.put(cache_key, result, now)
    return _para_llm(result)


# ── ver_trending ──────────────────────────────────────────────────────────────

async def _ver_trending(*, client: httpx.AsyncClient) -> list[dict]:
    """Items trending por volumen 24h (desde Supabase)."""
    from steam.rankings_repo import trending_repo
    from steam.mappers.row_mapper import _row_to_item

    rows = await trending_repo.fetch_snapshot()
    return _para_llm([_row_to_item(row) for row in rows])


# ── ver_movers ────────────────────────────────────────────────────────────────

async def _ver_movers(*, client: httpx.AsyncClient) -> dict:
    """Top movers (hot & cold) del mercado CS2 24h."""
    from steam.rankings_repo import movers_repo
    from steam.mappers.row_mapper import _row_to_item

    rows = await movers_repo.fetch_snapshot()
    hot = _para_llm([_row_to_item(r) for r in rows if r.get("bucket") == "hot"])
    cold = _para_llm([_row_to_item(r) for r in rows if r.get("bucket") == "cold"])
    return {"hot": hot, "cold": cold}


# ── historial_precio ──────────────────────────────────────────────────────────

async def _historial_precio(
    *, market_hash_name: str, client: httpx.AsyncClient, market: str = "csfloat", days: int = 35
) -> list[dict]:
    """Historial de precios de una skin."""
    from steam.services import pricing

    try:
        return await pricing.fetch_history_for_item(client, market_hash_name, limiter_timeout=CHAT_LIMITER_TIMEOUT)
    except HistoryBusy:
        # Vuelve al modelo como functionResponse: lo explica con sus palabras.
        return {"error": HISTORY_BUSY_MSG}


# ── Registrar todas las tools ─────────────────────────────────────────────────

def register_market_tools() -> None:
    """Registra las 5 tools de mercado en el registry."""
    register_tool(
        name="consultar_precio_skin",
        description=(
            "Obtiene el precio detallado de una skin de CS2 por su market hash name exacto. "
            "Incluye precio actual, deltas 24h/7d/30d, score de liquidez y datos de mercado."
        ),
        parameters={
            "type": "object",
            "properties": {
                "market_hash_name": {
                    "type": "string",
                    "description": "Market hash name canónico, ej: 'AK-47 | Redline (Field-Tested)'",
                },
            },
            "required": ["market_hash_name"],
        },
        fn=_consultar_precio_skin,
    )

    register_tool(
        name="buscar_skin",
        description=(
            "Busca skins de CS2 por nombre (parcial o completo). "
            "Devuelve hasta 10 resultados con precio, deltas e imagen."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Texto de búsqueda, ej: 'AK Redline' o 'Karambit Doppler'",
                },
            },
            "required": ["query"],
        },
        fn=_buscar_skin,
    )

    register_tool(
        name="ver_trending",
        description="Muestra los items trending del mercado CS2 por volumen de trading en las últimas 24 horas.",
        parameters={"type": "object", "properties": {}},
        fn=_ver_trending,
    )

    register_tool(
        name="ver_movers",
        description=(
            "Muestra los top movers del mercado CS2: los items que más suben (hot) "
            "y los que más bajan (cold) en las últimas 24 horas."
        ),
        parameters={"type": "object", "properties": {}},
        fn=_ver_movers,
    )

    register_tool(
        name="historial_precio",
        description=(
            "Obtiene el historial de precios de una skin de CS2. "
            "Útil para ver la evolución de precio en el tiempo."
        ),
        parameters={
            "type": "object",
            "properties": {
                "market_hash_name": {
                    "type": "string",
                    "description": "Market hash name canónico, ej: 'AK-47 | Redline (Field-Tested)'",
                },
            },
            "required": ["market_hash_name"],
        },
        fn=_historial_precio,
    )
