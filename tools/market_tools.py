"""Tools de mercado para el orquestador de Sharky.

Cada tool es un wrapper ligero sobre la lógica existente en
``steam/services.py`` y ``steam/routes/market.py``. No duplica lógica —
importa y llama funciones existentes.
"""

from __future__ import annotations

import logging

import httpx

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


def _para_llm(items: list[dict], limite: int = _TOP_ITEMS_LLM) -> list[dict]:
    """Proyecta los items a los campos que el modelo usa, y los recorta.

    Sin esto, `ver_movers` mete 20 items × 29 campos (17 KB) en el contexto y
    ahí se quedan para todas las vueltas siguientes del loop de tools.
    """
    return [
        {k: it[k] for k in _CAMPOS_LLM if it.get(k) is not None}
        for it in items[:limite]
    ]


# ── consultar_precio_skin ─────────────────────────────────────────────────────

async def _consultar_precio_skin(*, market_hash_name: str, client: httpx.AsyncClient) -> dict:
    """Devuelve precio detallado de una skin por nombre exacto."""
    from settings import STEAM_API_KEY
    from stores import _search_cache, SEARCH_CACHE_TTL, _item_price_cache, ITEM_PRICE_CACHE_TTL
    from steam.services import (
        STEAM_WEB_API,
        _enrich_prices,
        _enrich_market_prices,
        _cache_images,
        _fetch_static_images,
        _enrich_images_from_cache,
    )
    from steam.mappers import _map_item

    import time

    query = market_hash_name.strip()
    cache_key = query.lower()
    now = time.monotonic()

    # Cache de precio individual
    cached = _item_price_cache.get(cache_key)
    if cached and now - cached[1] < ITEM_PRICE_CACHE_TTL:
        return cached[0]

    client_http: httpx.AsyncClient = client
    resp = await client_http.get(
        f"{STEAM_WEB_API}/items",
        params={
            "key": STEAM_API_KEY,
            "game": "cs2",
            "search": query,
            "max": 30,
            "select": "id,marketname,markethashname,slug,image,pricelatestsell,pricereal,pricereal24h,pricereal7d,pricereal30d,color,bordercolor,rarity,quality,isstattrak,issouvenir,isstar,itemtype,itemname,tag5,sold24h,sold7d,sold30d,soldtotal,pricesafe,pricemin,pricemax,offervolume,buyordervolume,buyorderprice,prices,hourstosold,marketable,tradable,markettradablerestriction,steamurl,minfloat,maxfloat,paintindex",
            "format": "json",
            "production": "1",
        },
        timeout=15.0,
    )
    resp.raise_for_status()

    data = resp.json()
    if not isinstance(data, list):
        return {"error": "formato inesperado de Steam API"}

    raw = next(
        (r for r in data if (r.get("markethashname") or r.get("marketname") or "").lower() == cache_key),
        None,
    )
    if raw is None:
        return {"error": f"skin '{query}' no encontrada"}

    _cache_images([raw])
    item = _map_item(raw)
    (item,) = await _enrich_prices(client_http, [item])
    (item,) = await _enrich_market_prices(client_http, [item])
    await _fetch_static_images(client_http)
    _enrich_images_from_cache([item])

    _item_price_cache[cache_key] = (item, now)
    return item


# ── buscar_skin ───────────────────────────────────────────────────────────────

async def _buscar_skin(*, query: str, client: httpx.AsyncClient) -> list[dict]:
    """Busca skins por nombre y devuelve resultados relevantes."""
    from settings import STEAM_API_KEY
    from stores import _search_cache, SEARCH_CACHE_TTL
    from steam.services import (
        STEAM_WEB_API,
        _enrich_market_prices,
        _cache_images,
        _fetch_static_images,
        _enrich_images_from_cache,
    )
    from steam.mappers import _map_item

    import time

    q = query.strip()
    if not q:
        return []

    cache_key = q.lower()
    now = time.monotonic()
    cached = _search_cache.get(cache_key)
    if cached and now - cached[1] < SEARCH_CACHE_TTL:
        return _para_llm(cached[0])

    resp = await client.get(
        f"{STEAM_WEB_API}/items",
        params={
            "key": STEAM_API_KEY,
            "game": "cs2",
            "search": q,
            "max": 10,
            "select": "id,marketname,markethashname,slug,image,pricelatestsell,pricereal,pricereal24h,pricereal7d,pricereal30d,color,bordercolor,rarity,quality,isstattrak,issouvenir,isstar,itemtype,itemname,tag5,sold24h",
            "format": "json",
            "production": "1",
        },
        timeout=15.0,
    )
    resp.raise_for_status()

    data = resp.json()
    if not isinstance(data, list):
        return []

    _cache_images(data)
    result = [
        _map_item(raw) for raw in data
        if float(raw.get("pricelatestsell") or 0) > 0
        and "sticker slab" not in (raw.get("marketname") or raw.get("market_hash_name") or "").lower()
    ][:10]

    await _fetch_static_images(client)
    result = await _enrich_market_prices(client, result)
    _enrich_images_from_cache(result)

    # El cache guarda el item completo (lo consumen otros callers); la proyección
    # es solo para lo que ve el modelo.
    _search_cache[cache_key] = (result, now)
    return _para_llm(result)


# ── ver_trending ──────────────────────────────────────────────────────────────

async def _ver_trending(*, client: httpx.AsyncClient) -> list[dict]:
    """Items trending por volumen 24h (desde Supabase)."""
    from steam.rankings_repo import trending_repo
    from steam.market_rows import _row_to_item

    rows = await trending_repo.fetch_snapshot()
    return _para_llm([_row_to_item(row) for row in rows])


# ── ver_movers ────────────────────────────────────────────────────────────────

async def _ver_movers(*, client: httpx.AsyncClient) -> dict:
    """Top movers (hot & cold) del mercado CS2 24h."""
    from steam.rankings_repo import movers_repo
    from steam.market_rows import _row_to_item

    rows = await movers_repo.fetch_snapshot()
    hot = _para_llm([_row_to_item(r) for r in rows if r.get("bucket") == "hot"])
    cold = _para_llm([_row_to_item(r) for r in rows if r.get("bucket") == "cold"])
    return {"hot": hot, "cold": cold}


# ── historial_precio ──────────────────────────────────────────────────────────

async def _historial_precio(
    *, market_hash_name: str, client: httpx.AsyncClient, market: str = "csfloat", days: int = 35
) -> list[dict]:
    """Historial de precios de una skin."""
    from steam.services import _fetch_history_for_item

    pts = await _fetch_history_for_item(client, market_hash_name)
    return pts


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
