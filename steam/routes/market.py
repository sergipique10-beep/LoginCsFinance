import logging
import secrets
import time
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from settings import STEAM_API_KEY, CAP_TICK_TOKEN, PRICE_TICK_TOKEN, TRENDING_TRACK_TOP
from stores import (
    MARKET_INDEX_CACHE_TTL,
    SEARCH_CACHE_TTL, MARKET_PRICES_CACHE_TTL, ITEM_PRICE_CACHE_TTL,
    _market_index_cache, _topmovers_raw_cache,
    _search_cache, _market_prices_cache, _item_price_cache,
)
from auth.service import require_jwt
from ..cap_history_repo import insert_snapshot, fetch_range
from ..rankings_repo import trending_repo, movers_repo
from ..mappers import _map_item, _map_topmovers_item, _map_market_index_point
from ..market_rows import _to_row, _row_to_item
from ..services import (
    STEAM_WEB_API,
    STEAM_MARKET_API,
    _MOVERS_LIMIT,
    _enrich_prices,
    _enrich_market_prices,
    _fetch_market_providers,
    _cache_images,
    _enrich_images_from_cache,
    _fetch_static_images,
    _build_movers_from_topmovers,
)
from steam.price_capture import capture as price_capture_run

logger = logging.getLogger("uvicorn.error")

router = APIRouter()

_MOVERS_SELECT = ",".join([
    "id", "marketname", "markethashname", "slug", "image",
    "pricelatestsell",
    # Familia pricereal: la única con históricos reales por timeframe, de donde
    # _map_item calcula los deltas. Sin estos campos en el select, la API no los
    # devuelve y todos los resultados salen con badge "N/A".
    "pricereal", "pricereal24h", "pricereal7d", "pricereal30d",
    "color", "bordercolor", "rarity", "quality",
    "isstattrak", "issouvenir", "isstar",
    "itemtype", "itemname", "tag5",
    "sold24h", "sold7d", "sold30d", "soldtotal",
    "pricesafe", "pricemin", "pricemax",
    "offervolume", "buyordervolume", "buyorderprice",
    # `prices` alimenta el componente de consistencia entre mercados del Liquidity
    # Score. Sin este campo, el Market renormaliza sobre 0.90 y el mismo ítem puntúa
    # distinto que en el Inventario.
    "prices",
    "hourstosold", "marketable", "tradable",
    "markettradablerestriction", "steamurl",
    "minfloat", "maxfloat", "paintindex",
])

# Solo lo usa el fallback de topmovers (cuando /items falla), que trae ~20 items
# de un payload ya descargado. El ranking normal usa _TRENDING_CAPTURE_LIMIT.
_TRENDING_FALLBACK_LIMIT = 18
_SEARCH_LIMIT = 30

# Cuántos items persiste el trending-tick. NO cuesta cuota extra: /items es UNA
# petición sea cual sea el número de items que se guarden después (ver
# _ITEMS_FETCH_MAX). El coste lineal —una llamada a csfloat/history por item— ya
# no vive aquí: se separó a /internal/enrich-tick, que enriquece _ENRICH_BATCH
# por pasada avanzando como una rueda sobre la tabla. Los items todavía sin
# enriquecer conservan los deltas de _inline_delta (familia `pricereal`, gratis
# en el payload de /items), así que ninguna tarjeta se queda sin badge.
# 500 es el techo, no la expectativa: con max=5000 hay ~287 items ≥$10.80.
_TRENDING_CAPTURE_LIMIT = 500

# Cuántos items enriquece cada enrich-tick. Sigue siendo 18 porque es el límite
# del _history_limiter (18/60s) y una pasada tiene que caber en una ventana.
_ENRICH_BATCH = 18

# Días sin aparecer en una captura antes de purgar la fila. Sustituye al DELETE
# del replace-all; la ventana da margen a los items que entran y salen del
# ranking por fluctuaciones normales sin perder su enriquecimiento.
_TRENDING_STALE_DAYS = 7

# Suelo de precio para entrar en los rankings (USD; ~10 EUR a 1.08 USD/EUR).
# Por debajo, el movimiento porcentual es ruido de granularidad: los precios de
# Steam se mueven de centavo en centavo, así que en un item de $0.10 un solo tick
# ya es un +10% que ni el spread ni las comisiones (~15%) dejan capturar. Medido
# en producción: el 61% del trending eran items de ~$0.11 con volatilidad
# aparente 3.6x la de los de $2-10, y el agente los presentaba como "en auge".
_PRECIO_MIN_RANKING = 10.80

# Cuántos items pedir a /items. El endpoint ordena por unidades vendidas, y la
# cola barata es larguísima: la mediana de los 150 primeros es $0.20, así que con
# max=150 solo 2 items superaban los $10.80 y los rankings salían vacíos. Medido:
#   max=1000 →   7 items ≥$10.80    max=2000 →  47
#   max=1500 →  19                  max=5000 → 287
# Es UNA sola petición sea cual sea el valor: subirlo no consume más cuota del
# plan Starter (20 req/60s por endpoint). El coste es el payload (~9 MB) y la
# latencia (~1 s), ambos asumibles.
_ITEMS_FETCH_MAX = 5000

# Cuántos items como mucho de una misma categoría en el ranking. `_category_rank`
# ordena por prioridad de categoría, lo que AGOTA la primera antes de pasar a la
# siguiente: con "Rifle" en cabeza, los 18 huecos salían todos rifles (4 variantes
# de la misma skin incluidas). El material para diversificar existe — con
# max=5000 hay 113 rifles, 47 pistolas, 41 snipers, 18 SMG, 6 cuchillos — solo
# había que repartir en vez de ordenar.
_MAX_POR_CATEGORIA = 4

# Variantes de desgaste de una misma skin (Crane Flight FT/MW/WW/BS) son el mismo
# activo a efectos de "qué está pasando en el mercado". Tope aparte del de
# categoría, que no las distingue.
_MAX_POR_SKIN = 2


def _skin_base(nombre: str) -> str:
    """Nombre sin el desgaste: 'AK-47 | Crane Flight (Field-Tested)' → sin '(...)'."""
    return nombre.split(" (")[0].strip().lower()


def _diversificar(items: list[dict], limite: int) -> list[dict]:
    """Reparte el ranking entre categorías en vez de agotarlas por prioridad.

    Recorre los candidatos ya ordenados por relevancia y va aceptando mientras la
    categoría (y la skin base) no hayan llenado su cuota. Si al final sobran
    huecos —porque no hay bastante variedad— se rellenan con los descartados en
    orden, para no devolver una lista más corta de lo pedido.
    """
    aceptados: list[dict] = []
    # Solo se reservan para relleno los descartados por cuota de CATEGORÍA: una
    # lista corta es peor que una con dos rifles de más. Las variantes de desgaste
    # de una misma skin no vuelven nunca — cuatro Crane Flight no dicen nada que
    # no diga una, y ocupan el hueco de un activo distinto.
    relleno: list[dict] = []
    por_categoria: dict[str, int] = {}
    por_skin: dict[str, int] = {}

    # Las cuotas se calibraron para 18 huecos, donde su trabajo era evitar que
    # "Rifle" agotara la lista entera. A 500 esas mismas cifras descartarían
    # items que sí caben: con _MAX_POR_SKIN=2 y 5 desgastes por skin se tiraba
    # el 60% de las variantes. Escalan con el límite porque la diversificación
    # solo tiene que proteger la CABECERA, que es lo que se ve sin scroll.
    # A limite=18 (fallback) y limite=20 (movers) dan exactamente (4, 2) — el
    # comportamiento de hoy, sin regresión.
    max_categoria = max(_MAX_POR_CATEGORIA, limite // 8)
    max_skin = max(_MAX_POR_SKIN, limite // 100)

    for it in items:
        cat = it.get("weaponType") or "?"
        base = _skin_base(it.get("name") or "")
        if por_skin.get(base, 0) >= max_skin:
            continue
        if por_categoria.get(cat, 0) >= max_categoria:
            relleno.append(it)
            continue
        por_categoria[cat] = por_categoria.get(cat, 0) + 1
        por_skin[base] = por_skin.get(base, 0) + 1
        aceptados.append(it)
        if len(aceptados) >= limite:
            return aceptados

    return (aceptados + relleno)[:limite]


def _turnover(item: dict) -> float:
    """Facturación 24h estimada: precio × unidades vendidas.

    Criterio de relevancia para los rankings, en vez de las unidades sueltas.
    Ordenar por unidades premia lo barato por construcción — una Galil de $0.10
    vende más piezas que una AK de $28 aunque mueva 17x menos dinero.
    """
    return (item.get("priceLatest") or 0) * (item.get("sold24h") or 0)

_VALID_MARKETS = frozenset({
    "buff", "skinport", "skinbaron", "dmarket", "waxpeer",
    "bitskins", "csgotm", "haloskins", "tradeit", "skinbid",
    "csfloat", "youpin",
})


async def _compute_movers(client: httpx.AsyncClient) -> dict:
    """Calcula el ranking hot/cold actual (sin cache, sin persistencia).

    Llamado por POST /internal/movers-tick. GET /market/movers ahora lee
    el snapshot ya persistido en Supabase.
    """
    now = time.monotonic()

    # ── Primary source: /items (paid plan) ───────────────────────────────────
    try:
        resp = await client.get(
            f"{STEAM_WEB_API}/items",
            params={
                "key": STEAM_API_KEY,
                "game": "cs2",
                "sort_by": "soldZa",
                "max": _ITEMS_FETCH_MAX,
                "select": _MOVERS_SELECT,
                "format": "json",
                "production": "1",
            },
            timeout=15.0,
        )
        items_ok = resp.status_code == 200
    except (httpx.TimeoutException, httpx.RequestError):
        items_ok = False
        resp = None

    if items_ok and resp is not None:
        data = resp.json()
        if isinstance(data, list):
            _cache_images(data)
            mapped = []
            for raw in data:
                latest = float(raw.get("pricelatestsell") or 0)
                volume = int(raw.get("sold24h") or 0)
                if (latest >= _PRECIO_MIN_RANKING and volume >= 5
                        and "sticker slab" not in (raw.get("marketname") or "").lower()):
                    mapped.append(_map_item(raw))
            # Ordenar por turnover (precio × unidades), no por precio suelto: mide
            # qué mueve dinero de verdad. Cap at 20 (= _MOVERS_LIMIT * 2): exactly
            # the number of items displayed, and safe within the Starter plan's
            # 20 req/min rate limit.
            mapped.sort(key=_turnover, reverse=True)
            # Diversificar ANTES de enriquecer: _enrich_prices gasta una llamada
            # por item (limiter 18/60s), así que descartar después sería tirar
            # cuota en items que no se van a mostrar.
            candidates = _diversificar(mapped, _MOVERS_LIMIT * 2)
            logger.info("[market-movers] candidates: %d (capped from %d)", len(candidates), len(mapped))
            candidates = await _enrich_prices(client, candidates)
            with_delta = sorted(
                [x for x in candidates if x["priceDelta7d"] is not None],
                key=lambda x: x["priceDelta7d"],
            )
            no_delta = [x for x in candidates if x["priceDelta7d"] is None]
            logger.info("[market-movers] enriched=%d with_delta=%d no_delta=%d",
                        len(candidates), len(with_delta), len(no_delta))
            # hot = highest positive deltas; cold = lowest/most-negative deltas.
            # Fill remaining slots with no-delta items only if needed.
            hot  = list(reversed(with_delta[-_MOVERS_LIMIT:])) + no_delta
            cold = with_delta[:_MOVERS_LIMIT] + no_delta
            result = {
                "hot":  hot[:_MOVERS_LIMIT],
                "cold": cold[:_MOVERS_LIMIT],
            }
            # steamwebapi /items no devuelve `image` en este plan → el cache estático
            # (ByMykel) es la única fuente. Igual que en /market/items y /market/trending.
            await _fetch_static_images(client)
            _enrich_images_from_cache(result["hot"])
            _enrich_images_from_cache(result["cold"])
            await _enrich_market_prices(client, result["hot"])
            await _enrich_market_prices(client, result["cold"])
            return result
        logger.warning("[market-movers] /items returned unexpected type: %s", type(data).__name__)
    else:
        if resp is not None:
            logger.warning("[market-movers] /items returned %s — falling back to market-index topmovers", resp.status_code)

    # ── Fallback: market-index topmovers (free plan) ─────────────────────────
    raw_topmovers = _topmovers_raw_cache.get("latest")
    if not raw_topmovers:
        # topmovers cache is cold — fetch market-index now to populate it
        try:
            mi_resp = await client.get(
                f"{STEAM_WEB_API}/market-index/cs2",
                params={"key": STEAM_API_KEY, "format": "json"},
                timeout=15.0,
            )
            if mi_resp.status_code == 200:
                mi_data = mi_resp.json()
                if isinstance(mi_data, dict):
                    tm = mi_data.get("topmovers", {})
                    gainers = tm.get("gainers", [])
                    losers  = tm.get("losers", [])
                    if gainers:
                        logger.info("[market-movers] topmovers gainer keys: %s", list(gainers[0].keys()))
                        logger.info("[market-movers] topmovers gainer sample: %s", gainers[0])
                    _topmovers_raw_cache["latest"] = (gainers, losers, now)
                    raw_topmovers = _topmovers_raw_cache["latest"]
        except Exception as exc:
            logger.warning("[market-movers] could not fetch market-index for topmovers: %s", exc)

    if raw_topmovers:
        gainers, losers, _ = raw_topmovers
        result = _build_movers_from_topmovers(gainers, losers)
        if result:
            await _fetch_static_images(client)
            _enrich_images_from_cache(result["hot"])
            _enrich_images_from_cache(result["cold"])
            await _enrich_market_prices(client, result["hot"])
            await _enrich_market_prices(client, result["cold"])
            logger.info("[market-movers] serving from market-index topmovers (%d hot, %d cold)", len(result["hot"]), len(result["cold"]))
            return result

    logger.warning("[market-movers] no data available from any source")
    return {"hot": [], "cold": []}


@router.get("/market/movers", summary="Top movers del mercado CS2 (hot & cold 24 h)")
async def get_market_movers(request: Request, user: dict = Depends(require_jwt)):
    rows = await movers_repo.fetch_snapshot()
    hot  = [_row_to_item(r) for r in rows if r.get("bucket") == "hot"]
    cold = [_row_to_item(r) for r in rows if r.get("bucket") == "cold"]
    return {"hot": hot, "cold": cold}


@router.get("/market/items", summary="Busca items en el mercado CS2 por nombre")
async def get_market_items(
    request: Request,
    q: str,
    user: dict = Depends(require_jwt),
):
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="q is required")

    cache_key = query.lower()
    now = time.monotonic()
    cached = _search_cache.get(cache_key)
    if cached and now - cached[1] < SEARCH_CACHE_TTL:
        return cached[0]

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/items",
            params={
                "key": STEAM_API_KEY,
                "game": "cs2",
                "search": query,
                "max": _SEARCH_LIMIT,
                "select": _MOVERS_SELECT,
                "format": "json",
                "production": "1",
            },
            timeout=15.0,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code == 402:
        raise HTTPException(status_code=429, detail="Steam API daily limit reached — try again tomorrow")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()
    if not isinstance(data, list):
        raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

    _cache_images(data)
    result = [
        _map_item(raw) for raw in data
        if float(raw.get("pricelatestsell") or 0) > 0
        and "sticker slab" not in (raw.get("marketname") or raw.get("market_hash_name") or "").lower()
    ][:_SEARCH_LIMIT]

    await _fetch_static_images(request.app.state.http_client)
    result = await _enrich_market_prices(request.app.state.http_client, result)
    _enrich_images_from_cache(result)

    _search_cache[cache_key] = (result, now)
    logger.info("[market-items] q=%r → %d results", query, len(result))
    return result


@router.get("/market/price", summary="Datos completos (con liquidez) de un item CS2 por nombre")
async def get_market_price(
    request: Request,
    name: str,
    user: dict = Depends(require_jwt),
):
    """Item único con el shape completo de _map_item — incluye liquidityScore,
    liquidityBreakdown y el bloque de volumen.

    Existe porque /market/trending y /market/movers sirven snapshots de Supabase
    vía _row_to_item, que NO transporta esos campos. El detail sheet del frontend
    llama aquí al abrirse sobre un item de trending para no depender del snapshot.
    """
    query = name.strip()
    if not query:
        raise HTTPException(status_code=400, detail="name is required")

    cache_key = query.lower()
    now = time.monotonic()
    cached = _item_price_cache.get(cache_key)
    if cached and now - cached[1] < ITEM_PRICE_CACHE_TTL:
        return cached[0]

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/items",
            params={
                "key": STEAM_API_KEY,
                "game": "cs2",
                "search": query,
                "max": _SEARCH_LIMIT,
                "select": _MOVERS_SELECT,
                "format": "json",
                "production": "1",
            },
            timeout=15.0,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code == 402:
        raise HTTPException(status_code=429, detail="Steam API daily limit reached — try again tomorrow")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()
    if not isinstance(data, list):
        raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

    # steamwebapi /items?search es fuzzy: nos quedamos con el match exacto por
    # markethashname (el nombre canónico en inglés, el mismo que manda el frontend).
    raw = next(
        (r for r in data
         if (r.get("markethashname") or r.get("marketname") or "").lower() == cache_key),
        None,
    )
    if raw is None:
        raise HTTPException(status_code=404, detail=f"Item '{query}' not found")

    _cache_images([raw])
    item = _map_item(raw)
    (item,) = await _enrich_prices(request.app.state.http_client, [item])
    (item,) = await _enrich_market_prices(request.app.state.http_client, [item])
    await _fetch_static_images(request.app.state.http_client)
    _enrich_images_from_cache([item])

    _item_price_cache[cache_key] = (item, now)
    logger.info("[market-price] name=%r → hit", query)
    return item


async def _compute_trending(client: httpx.AsyncClient) -> list[dict]:
    """Calcula el ranking trending actual (sin cache, sin persistencia).

    Llamado tanto por GET /market/trending (antes de la migración a
    Supabase) como por POST /internal/trending-tick.
    """
    now = time.monotonic()

    # ── Primary source: /items (paid plan) ───────────────────────────────────
    try:
        resp = await client.get(
            f"{STEAM_WEB_API}/items",
            params={
                "key": STEAM_API_KEY,
                "game": "cs2",
                "sort_by": "soldZa",
                "max": _ITEMS_FETCH_MAX,
                "select": _MOVERS_SELECT,
                "format": "json",
                "production": "1",
            },
            timeout=15.0,
        )
        items_ok = resp.status_code == 200
    except (httpx.TimeoutException, httpx.RequestError):
        items_ok = False
        resp = None

    if items_ok and resp is not None:
        data = resp.json()
        if isinstance(data, list):
            _cache_images(data)
            result = []
            for raw in data:
                latest = float(raw.get("pricelatestsell") or 0)
                volume = int(raw.get("sold24h") or 0)
                if latest >= _PRECIO_MIN_RANKING and volume >= 1:
                    result.append(_map_item(raw))
            # Dentro de cada categoría, por turnover (precio × unidades) en vez de
            # por unidades: ordenar por piezas vendidas premia lo barato por
            # construcción y llenaba la lista de items de céntimos.
            # Por relevancia (turnover) y luego se reparte entre categorías. Antes
            # se ordenaba por _category_rank primero, lo que agotaba "Rifle" antes
            # de llegar a ninguna otra categoría.
            result = _diversificar(
                sorted(result, key=_turnover, reverse=True), _TRENDING_CAPTURE_LIMIT
            )
            # Sin _enrich_prices a propósito: es una llamada a csfloat/history
            # POR ITEM, el único coste que escala. Vive ahora en
            # /internal/enrich-tick, que rota sobre la tabla en pasadas de 18.
            # Hasta que a un item le toque la rueda, sus deltas son los de
            # _inline_delta (familia `pricereal`), que ya vienen en el payload.
            # _enrich_market_prices sí se queda: son 2 peticiones fijas y
            # cacheadas, no escalan con el número de items.
            result = await _enrich_market_prices(client, result)
            # steamwebapi /items no devuelve `image` en este plan → el cache estático
            # (ByMykel) es la única fuente. Igual que en /market/items (search).
            await _fetch_static_images(client)
            _enrich_images_from_cache(result)
            return result
        logger.warning("[market-trending] /items returned unexpected type: %s", type(data).__name__)
    else:
        if resp is not None:
            logger.warning("[market-trending] /items returned %s — falling back to topmovers", resp.status_code)

    # ── Fallback: topmovers from cache (free plan) ────────────────────────────
    raw_topmovers = _topmovers_raw_cache.get("latest")
    if not raw_topmovers:
        try:
            mi_resp = await client.get(
                f"{STEAM_WEB_API}/market-index/cs2",
                params={"key": STEAM_API_KEY, "format": "json"},
                timeout=15.0,
            )
            if mi_resp.status_code == 200:
                mi_data = mi_resp.json()
                if isinstance(mi_data, dict):
                    tm = mi_data.get("topmovers", {})
                    gainers = tm.get("gainers", [])
                    losers  = tm.get("losers", [])
                    _topmovers_raw_cache["latest"] = (gainers, losers, now)
                    raw_topmovers = _topmovers_raw_cache["latest"]
        except Exception as exc:
            logger.warning("[market-trending] could not fetch market-index for topmovers: %s", exc)

    if raw_topmovers:
        gainers, losers, _ = raw_topmovers
        combined = gainers + losers
        if combined:
            await _fetch_static_images(client)
            result = [_map_topmovers_item(item) for item in combined]
            _enrich_images_from_cache(result)
            result = sorted(result, key=lambda x: x["sold24h"], reverse=True)[:_TRENDING_FALLBACK_LIMIT]
            result = await _enrich_market_prices(client, result)
            logger.info("[market-trending] serving from topmovers (%d items)", len(result))
            return result

    logger.warning("[market-trending] no data available from any source")
    return []


@router.get("/market/trending", summary="Items trending del mercado CS2 (por volumen 24h)")
async def get_market_trending(request: Request, user: dict = Depends(require_jwt)):
    # Por turnover y no por `rank`: con upsert, un item que no aparece en una
    # captura conserva su rank viejo y ocuparía una posición alta como fantasma
    # hasta que la purga se lo lleve. El turnover se actualiza en cada captura.
    rows = await trending_repo.fetch_ranked()
    return [_row_to_item(row) for row in rows]


@router.get("/market/index", summary="Índice de mercado global CS2")
async def get_market_index(
    request: Request,
    tf: str = "24h",
    user: dict = Depends(require_jwt),
):
    cache_key = tf
    now = time.monotonic()
    cached = _market_index_cache.get(cache_key)
    if cached and now - cached[1] < MARKET_INDEX_CACHE_TTL:
        return cached[0]

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/market-index/cs2",
            params={"key": STEAM_API_KEY, "format": "json"},
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Market index request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code == 402:
        logger.warning("[market-index] daily limit reached (402)")
        raise HTTPException(status_code=429, detail="Steam API daily limit reached — try again tomorrow")
    if resp.status_code != 200:
        logger.error("[market-index] steamwebapi returned %s | body: %s", resp.status_code, resp.text[:500])
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()

    if isinstance(data, list):
        raw_points = data
        delta_24h = 0.0
        top = None
        turnover24h = 0.0
        sold24h = 0
    elif isinstance(data, dict):
        history = data.get("history", [])
        if isinstance(history, list):
            raw_points = history
        elif isinstance(history, dict):
            raw_points = history.get("priceindex", [])
            if not isinstance(raw_points, list):
                logger.error("[market-index] 'priceindex' unexpected type: %s", type(raw_points).__name__)
                raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")
        else:
            logger.error("[market-index] 'history' unexpected type: %s | sample: %s", type(history).__name__, str(history)[:200])
            raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

        changes_24h = data.get("changes", {}).get("24h", {})
        delta_24h = 0.0
        if isinstance(changes_24h, dict):
            pi_change = changes_24h.get("priceindex", {})
            if isinstance(pi_change, dict):
                delta_24h = float(pi_change.get("change") or 0)

        topmovers = data.get("topmovers", {})
        gainers = topmovers.get("gainers", [])
        losers  = topmovers.get("losers", [])
        top = gainers[0] if gainers else None
        if gainers:
            logger.info("[market-index] topmovers gainer keys: %s", list(gainers[0].keys()))
            logger.info("[market-index] topmovers gainer sample: %s", gainers[0])
        _topmovers_raw_cache["latest"] = (gainers, losers, now)
        turnover24h = float(data.get("turnover24h") or 0)
        sold24h = int(data.get("sold24h") or 0)
    else:
        logger.error("[market-index] unexpected top-level type: %s", type(data).__name__)
        raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

    result = {
        "turnover24h": turnover24h,
        "sold24h": sold24h,
        "delta24h": delta_24h,
        "hottestItem": {
            "name": top["markethashname"] if top else "—",
            "change24h": float(top["change24h"]) if top else 0.0,
        },
        "history": [_map_market_index_point(p) for p in raw_points],
    }
    _market_index_cache[cache_key] = (result, now)
    return result


_CAP_TF_MAP: dict[str, timedelta] = {
    "7d":  timedelta(days=7),
    "1m":  timedelta(days=30),
    "3m":  timedelta(days=90),
    "6m":  timedelta(days=180),
    "1y":  timedelta(days=365),
    "3y":  timedelta(days=1095),
}

# Tamaño de bucket de downsampling por timeframe. A 3 años de snapshots
# horarios serían ~26k puntos crudos; agrupando se mantiene el payload acotado.
_CAP_BUCKET_MAP: dict[str, timedelta] = {
    "7d":  timedelta(hours=1),
    "1m":  timedelta(hours=6),
    "3m":  timedelta(days=1),
    "6m":  timedelta(days=1),
    "1y":  timedelta(weeks=1),
    "3y":  timedelta(weeks=1),
}

_CAP_FIELDS = ("priceindex", "realpriceindex", "buyorderpriceindex", "turnover24h")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_ts(ts: str) -> datetime:
    """Parsea un timestamptz ISO (con 'Z' o offset) a datetime aware en UTC."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _downsample(rows: list[dict], bucket: timedelta) -> list[dict]:
    """
    Agrupa filas por floor(ts / bucket) y promedia cada campo.
    Mantiene { ts, v } (v = priceindex) y añade real/buyorder/turnover medios.
    `ts` de salida = inicio del bucket. Asume `rows` ordenado por ts asc.
    """
    bucket_s = bucket.total_seconds()
    grouped: dict[float, list[dict]] = {}
    order: list[float] = []
    for row in rows:
        ts = _parse_ts(row["ts"])
        idx = (ts - _EPOCH).total_seconds() // bucket_s
        if idx not in grouped:
            grouped[idx] = []
            order.append(idx)
        grouped[idx].append(row)

    out: list[dict] = []
    for idx in order:
        members = grouped[idx]
        start = _EPOCH + timedelta(seconds=idx * bucket_s)
        point: dict = {"ts": start.isoformat().replace("+00:00", "Z")}
        for field in _CAP_FIELDS:
            vals = [m[field] for m in members if m.get(field) is not None]
            point[field] = sum(vals) / len(vals) if vals else None
        # Contrato con el frontend: v = priceindex.
        point["v"] = point["priceindex"]
        out.append(point)
    return out


@router.post("/internal/cap-tick", summary="Captura un snapshot del índice de precio CS2 (cron interno)")
async def cap_tick(
    request: Request,
    x_cap_token: str | None = Header(default=None),
):
    if not CAP_TICK_TOKEN or not x_cap_token or not secrets.compare_digest(x_cap_token, CAP_TICK_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing cap-tick token")

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/market-index/cs2",
            params={"key": STEAM_API_KEY, "format": "json"},
            timeout=15.0,
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code != 200:
        logger.warning("[cap-tick] market-index returned %s", resp.status_code)
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

    price_index = data.get("priceindex")
    if price_index is None:
        logger.warning("[cap-tick] 'priceindex' missing from response")
        raise HTTPException(status_code=502, detail="'priceindex' missing from Steam response")

    def _num(value):
        return float(value) if value is not None else None

    # Floor al inicio de la hora: la PK es `ts`, así que varias capturas dentro
    # de la misma hora colapsan en una sola fila (upsert idempotente).
    hour_ts = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    point = {
        "ts": hour_ts.isoformat().replace("+00:00", "Z"),
        "priceindex": float(price_index or 0),
        "realpriceindex": _num(data.get("realpriceindex")),
        "buyorderpriceindex": _num(data.get("buyorderpriceindex")),
        "turnover24h": _num(data.get("turnover24h")),
    }

    await insert_snapshot(point)
    logger.info("[cap-tick] snapshot saved: %s = %.4f", point["ts"], point["priceindex"])
    return {"ok": True, "ts": point["ts"], "priceindex": point["priceindex"]}


@router.get("/market/cap-history", summary="Historial del índice de precio CS2 (snapshots horarios)")
async def get_market_cap_history(
    tf: str = "7d",
    user: dict = Depends(require_jwt),
):
    if tf not in _CAP_TF_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tf '{tf}'. Valid values: {', '.join(_CAP_TF_MAP)}",
        )
    cutoff = datetime.now(timezone.utc) - _CAP_TF_MAP[tf]
    rows = await fetch_range(cutoff)
    return _downsample(rows, _CAP_BUCKET_MAP[tf])


@router.post("/internal/trending-tick", summary="Captura el ranking trending del mercado CS2 (cron interno)")
async def trending_tick(
    request: Request,
    x_cap_token: str | None = Header(default=None),
):
    if not CAP_TICK_TOKEN or not x_cap_token or not secrets.compare_digest(x_cap_token, CAP_TICK_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing cap-tick token")

    items = await _compute_trending(request.app.state.http_client)
    seen_at = datetime.now(timezone.utc).isoformat()
    # Upsert, no replace-all: el DELETE borraría el enriquecimiento que el
    # enrich-tick va acumulando en price_delta_* / enriched_at. Estas filas no
    # llevan esas dos claves, así que PostgREST no las toca.
    rows = [{**_to_row(item, rank), "seen_at": seen_at}
            for rank, item in enumerate(items)]
    await trending_repo.upsert_rows(rows)
    # Sin el DELETE, los items que salen del ranking se quedarían para siempre.
    purged = await trending_repo.purge_stale(_TRENDING_STALE_DAYS)

    # Los items del ranking no tenían serie propia en precios_historicos, así
    # que cualquier predicción sobre ellos caía a CSFloat. Registrar el top N
    # por turnover (`items` ya viene ordenado así desde _diversificar) los mete
    # en la rueda del price-tick. Es un upsert con ignore_duplicates: repetirlo
    # cada hora no pisa `first_seen` ni `last_captured`.
    # Best-effort como en /inventory: que falle el registro no puede tumbar la
    # captura del ranking, que es lo que sirve la pantalla.
    tracked = 0
    try:
        from steam.price_history_repo import register_tracked
        nombres = [i["name"] for i in items[:TRENDING_TRACK_TOP] if i.get("name")]
        if nombres:
            await register_tracked(nombres, "trending")
            tracked = len(nombres)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[trending-tick] register_tracked falló: %s", exc)

    logger.info("[trending-tick] upserted=%d purged=%d tracked=%d",
                len(rows), purged, tracked)
    return {"ok": True, "count": len(rows), "purged": purged, "tracked": tracked}


@router.post("/internal/enrich-tick", summary="Enriquece deltas del trending con histórico csfloat (cron interno)")
async def enrich_tick(request: Request, x_cap_token: str | None = Header(default=None)):
    """Avanza la rueda de enriquecimiento: coge los N items menos-recientemente
    enriquecidos y les recalcula los deltas desde el histórico de csfloat.

    Es el único coste que escala con el número de items (1 req por item), por
    eso está separado de la captura y capado a _ENRICH_BATCH por pasada.
    """
    if not CAP_TICK_TOKEN or not x_cap_token or not secrets.compare_digest(x_cap_token, CAP_TICK_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing cap-tick token")

    names = await trending_repo.fetch_stalest(_ENRICH_BATCH)
    if not names:
        return {"ok": True, "count": 0, "with_deltas": 0}

    # _enrich_prices solo lee item["name"] y sobrescribe los tres deltas, así
    # que no hace falta cargar la fila entera de Supabase.
    stubs = [{"name": n, "priceDelta24h": None, "priceDelta7d": None, "priceDelta30d": None}
             for n in names]
    enriched = await _enrich_prices(request.app.state.http_client, stubs)

    enriched_at = datetime.now(timezone.utc).isoformat()
    con_deltas: list[dict] = []
    sin_deltas: list[dict] = []
    for e in enriched:
        # Si csfloat no devolvió histórico, _enrich_prices deja el stub intacto
        # y los tres deltas siguen a None. Escribirlos pisaría con null el delta
        # bueno que _inline_delta dejó en la captura — así que en ese caso solo
        # se marca la fila como intentada.
        if e["priceDelta24h"] is None and e["priceDelta7d"] is None and e["priceDelta30d"] is None:
            sin_deltas.append({"name": e["name"], "enriched_at": enriched_at})
        else:
            con_deltas.append({
                "name": e["name"],
                "price_delta_24h": e["priceDelta24h"],
                "price_delta_7d": e["priceDelta7d"],
                "price_delta_30d": e["priceDelta30d"],
                "enriched_at": enriched_at,
            })

    # Dos llamadas y no una: PostgREST rellena con null las claves que falten en
    # unas filas y estén en otras del mismo lote. Mezclarlas borraría deltas.
    await trending_repo.upsert_rows(con_deltas)
    await trending_repo.upsert_rows(sin_deltas)

    # enriched_at se escribe SIEMPRE, con o sin datos: si no, un item sin
    # histórico en csfloat se quedaría con enriched_at null para siempre y
    # acapararía la rueda cada 15 min, quemando la cuota en los mismos muertos.
    logger.info("[enrich-tick] intentados=%d con_deltas=%d", len(names), len(con_deltas))
    return {"ok": True, "count": len(names), "with_deltas": len(con_deltas)}


@router.post("/internal/movers-tick", summary="Captura el ranking hot/cold del mercado CS2 (cron interno)")
async def movers_tick(request: Request, x_cap_token: str | None = Header(default=None)):
    if not CAP_TICK_TOKEN or not x_cap_token or not secrets.compare_digest(x_cap_token, CAP_TICK_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing cap-tick token")
    result = await _compute_movers(request.app.state.http_client)
    rows = [_to_row(item, rank, "hot")  for rank, item in enumerate(result["hot"])] \
         + [_to_row(item, rank, "cold") for rank, item in enumerate(result["cold"])]
    await movers_repo.replace_snapshot(rows)
    logger.info("[movers-tick] snapshot saved: %d items", len(rows))
    return {"ok": True, "count": len(rows)}


@router.post("/internal/price-tick", summary="Captura diaria de precios por-skin (cron)")
async def price_tick(
    request: Request,
    x_price_tick_token: str = Header(default=""),
):
    if not PRICE_TICK_TOKEN or not secrets.compare_digest(
        x_price_tick_token.encode(), PRICE_TICK_TOKEN.encode()
    ):
        raise HTTPException(status_code=401, detail="Token inválido")
    return await price_capture_run(request.app.state.http_client)


@router.get("/market/providers", summary="Lista de markets soportados como price providers")
async def get_market_providers(request: Request, user: dict = Depends(require_jwt)):
    providers = await _fetch_market_providers(request.app.state.http_client)
    return providers


@router.get("/market/prices", summary="Precios en tiempo real de un item por mercado")
async def get_market_prices(
    request: Request,
    market: str,
    name: str | None = None,
    currency: str | None = None,
    user: dict = Depends(require_jwt),
):
    market = market.lower().strip()
    if market not in _VALID_MARKETS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown market '{market}'. Valid: {', '.join(sorted(_VALID_MARKETS))}",
        )

    cache_key = f"{market}:{(name or '').lower()}:{(currency or 'usd').lower()}"
    now = time.monotonic()
    cached = _market_prices_cache.get(cache_key)
    if cached and now - cached[1] < MARKET_PRICES_CACHE_TTL:
        return cached[0]

    params: dict = {"key": STEAM_API_KEY}
    if name:
        params["market_hash_name"] = name
    if currency:
        params["currency"] = currency

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_MARKET_API}/{market}/prices",
            params=params,
            timeout=15.0,
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Market prices request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code == 402:
        raise HTTPException(status_code=429, detail="Steam API daily limit reached — try again tomorrow")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Market '{market}' not found or no prices available")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()
    _market_prices_cache[cache_key] = (data, now)
    logger.info("[market-prices] market=%r name=%r → %s items", market, name, len(data) if isinstance(data, list) else "object")
    return data
