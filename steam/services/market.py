"""Orquestación del mercado (CLEAN-11): rankings hot/cold y trending, búsqueda, item
completo, índice de mercado, precios por mercado, histórico del índice y los ticks que
los persisten. Sin FastAPI: lanza los errores de `steam/errors.py` y las rutas los
traducen a HTTP.
"""
import logging
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

import httpx

from settings import TRENDING_TRACK_TOP
from stores import (
    _item_price_cache, _market_index_cache, _market_prices_cache, _search_cache,
    _topmovers_raw_cache,
)
from steam.cap_history_repo import fetch_range, insert_snapshot
from steam.clients import steamwebapi
from steam.errors.handling import log_degraded, reason_of
from steam.domain.models import Fetched, RankedCard, SkinCard
from steam.domain.names import is_sticker_slab, skin_base
from steam.domain.validators import MIN_SOLD_MOVERS, MIN_SOLD_TRENDING, ranking_eligible
from steam.errors import (
    UNEXPECTED_FORMAT, QuotaExhausted, SourceTimeout, SourceUnavailable, UnexpectedPayload,
    UpstreamError,
)
from steam.mappers.items import _map_item
from steam.mappers.market_index import _map_market_index_point
from steam.mappers.movers import _MOVERS_LIMIT, _build_movers_from_topmovers, _map_topmovers_item
from steam.mappers.rows import _row_to_item, _to_row
from steam.rankings_repo import movers_repo, trending_repo
from steam.services import catalog, pricing

logger = logging.getLogger("uvicorn.error")

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

# Cuántos items pedir a /items. El endpoint ordena por unidades vendidas, y la
# cola barata es larguísima: la mediana de los 150 primeros es $0.20, así que con
# max=150 solo 2 items superaban los $10.80 y los rankings salían vacíos. Medido:
#   max=1000 →   7 items ≥$10.80    max=2000 →  47
#   max=1500 →  19                  max=5000 → 287
# Es UNA sola petición sea cual sea el valor: subirlo no consume más cuota del
# plan Starter (20 req/60s por endpoint). El coste es el payload (~9 MB) y la
# latencia (~1 s), ambos asumibles.
_ITEMS_FETCH_MAX = 5000

# Cuántos items como mucho de una misma categoría en el ranking. Ordenar por
# prioridad de categoría (el antiguo `_category_rank`) AGOTA la primera antes de pasar a la
# siguiente: con "Rifle" en cabeza, los 18 huecos salían todos rifles (4 variantes
# de la misma skin incluidas). El material para diversificar existe — con
# max=5000 hay 113 rifles, 47 pistolas, 41 snipers, 18 SMG, 6 cuchillos — solo
# había que repartir en vez de ordenar.
_MAX_POR_CATEGORIA = 4

# Variantes de desgaste de una misma skin (Crane Flight FT/MW/WW/BS) son el mismo
# activo a efectos de "qué está pasando en el mercado". Tope aparte del de
# categoría, que no las distingue.
_MAX_POR_SKIN = 2


_Card = TypeVar("_Card", bound=Mapping[str, Any])
_T = TypeVar("_T")

def _diversificar(items: list[_Card], limite: int) -> list[_Card]:
    """Reparte el ranking entre categorías en vez de agotarlas por prioridad.

    Recorre los candidatos ya ordenados por relevancia y va aceptando mientras la
    categoría (y la skin base) no hayan llenado su cuota. Si al final sobran
    huecos —porque no hay bastante variedad— se rellenan con los descartados en
    orden, para no devolver una lista más corta de lo pedido.
    """
    aceptados: list[_Card] = []
    # Solo se reservan para relleno los descartados por cuota de CATEGORÍA: una
    # lista corta es peor que una con dos rifles de más. Las variantes de desgaste
    # de una misma skin no vuelven nunca — cuatro Crane Flight no dicen nada que
    # no diga una, y ocupan el hueco de un activo distinto.
    relleno: list[_Card] = []
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
        base = skin_base(it.get("name") or "")
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

def _turnover(item: Mapping[str, Any]) -> float:
    """Facturación 24h estimada: precio × unidades vendidas.

    Criterio de relevancia para los rankings, en vez de las unidades sueltas.
    Ordenar por unidades premia lo barato por construcción — una Galil de $0.10
    vende más piezas que una AK de $28 aunque mueva 17x menos dinero.
    """
    return (item.get("priceLatest") or 0) * (item.get("sold24h") or 0)


# ── Fuentes de los rankings ───────────────────────────────────────────────────

async def search_items(client: httpx.AsyncClient, query: str, *, max: int, select: str) -> Any:
    """La búsqueda en /items, una sola implementación para /market/items, /market/price
    y las dos tools del chat. Cada llamador conserva su `max`, su `select` y qué hace
    con un cuerpo que no es lista (CAL-14 decidirá si se unifican)."""
    return await steamwebapi.items(client, search=query, max=max, select=select)


async def _ranking_items(client: httpx.AsyncClient, tag: str,
                         fallback_label: str) -> tuple[list | None, str | None]:
    """Fuente principal de movers y trending: /items por unidades vendidas. Devuelve
    (items, None), o (None, motivo) si no responde o no es una lista; entonces se cae
    a topmovers."""
    try:
        data = await steamwebapi.items(
            client, sort_by="soldZa", max=_ITEMS_FETCH_MAX, select=_MOVERS_SELECT,
        )
    except (SourceTimeout, SourceUnavailable) as exc:
        return None, reason_of(exc)
    except UpstreamError as exc:
        logger.warning("[%s] /items returned %s — falling back to %s", tag, exc.status, fallback_label)
        return None, reason_of(exc)
    if isinstance(data, list):
        return data, None
    logger.warning("[%s] /items returned unexpected type: %s", tag, type(data).__name__)
    return None, "unexpected_format"


async def _topmovers(client: httpx.AsyncClient, tag: str, now: float, *,
                     log_sample: bool) -> tuple[list, list] | None:
    """Respaldo: topmovers de market-index. Se usa lo último cacheado, tenga la edad que
    tenga (CAL-12); si no hay nada, se pide market-index ahora para llenarlo."""
    raw_topmovers = _topmovers_raw_cache.stale("latest")
    if not raw_topmovers:
        try:
            mi_data = await steamwebapi.market_index(client, timeout=15.0)
            if isinstance(mi_data, dict):
                tm = mi_data.get("topmovers", {})
                gainers = tm.get("gainers", [])
                losers  = tm.get("losers", [])
                if log_sample and gainers:
                    logger.info("[%s] topmovers gainer keys: %s", tag, list(gainers[0].keys()))
                    logger.info("[%s] topmovers gainer sample: %s", tag, gainers[0])
                _topmovers_raw_cache.put("latest", (gainers, losers), now)
                raw_topmovers = (gainers, losers)
        except Exception as exc:
            # Un status distinto de 200 se salta en silencio, como antes del cliente.
            if not (isinstance(exc, UpstreamError) and exc.status is not None):
                logger.warning("[%s] could not fetch market-index for topmovers: %s", tag, exc)
    return raw_topmovers


async def compute_movers(client: httpx.AsyncClient) -> Fetched[dict]:
    """Calcula el ranking hot/cold actual (sin cache, sin persistencia).

    Llamado por POST /internal/movers-tick. GET /market/movers ahora lee
    el snapshot ya persistido en Supabase. `partial` = respaldo de topmovers;
    `error` = ninguna fuente (listas vacías).
    """
    now = time.monotonic()

    # ── Primary source: /items (paid plan) ───────────────────────────────────
    data, reason = await _ranking_items(client, "market-movers", "market-index topmovers")
    if data is not None:
        catalog.cache_images(data)
        mapped = []
        for raw in data:
            if (ranking_eligible(raw, MIN_SOLD_MOVERS)
                    and not is_sticker_slab(raw.get("marketname") or "")):
                mapped.append(_map_item(raw))
        # Ordenar por turnover (precio × unidades), no por precio suelto: mide
        # qué mueve dinero de verdad. Cap at 20 (= _MOVERS_LIMIT * 2): exactly
        # the number of items displayed, and safe within the Starter plan's
        # 20 req/min rate limit.
        mapped.sort(key=_turnover, reverse=True)
        # Diversificar ANTES de enriquecer: enrich_prices gasta una llamada
        # por item (limiter 18/60s), así que descartar después sería tirar
        # cuota en items que no se van a mostrar.
        # list[Any]: enrich_prices devuelve dicts nuevos sin tipar.
        candidates: list[Any] = _diversificar(mapped, _MOVERS_LIMIT * 2)
        logger.info("[market-movers] candidates: %d (capped from %d)", len(candidates), len(mapped))
        candidates = await pricing.enrich_prices(client, candidates)
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
        await catalog.fetch_static_images(client)
        catalog.enrich_images_from_cache(result["hot"])
        catalog.enrich_images_from_cache(result["cold"])
        await pricing.enrich_market_prices(client, result["hot"])
        await pricing.enrich_market_prices(client, result["cold"])
        return Fetched(result)

    # ── Fallback: market-index topmovers (free plan) ─────────────────────────
    raw_topmovers = await _topmovers(client, "market-movers", now, log_sample=True)
    if raw_topmovers:
        gainers, losers = raw_topmovers
        fallback = _build_movers_from_topmovers(gainers, losers)
        if fallback:
            await catalog.fetch_static_images(client)
            catalog.enrich_images_from_cache(fallback["hot"])
            catalog.enrich_images_from_cache(fallback["cold"])
            await pricing.enrich_market_prices(client, fallback["hot"])
            await pricing.enrich_market_prices(client, fallback["cold"])
            logger.info("[market-movers] serving from market-index topmovers (%d hot, %d cold)", len(fallback["hot"]), len(fallback["cold"]))
            log_degraded("movers", reason or "unknown", "fallback")
            return Fetched(fallback, "partial", reason)

    logger.warning("[market-movers] no data available from any source")
    log_degraded("movers", reason or "unknown", "error")
    return Fetched({"hot": [], "cold": []}, "error", reason)


async def compute_trending(client: httpx.AsyncClient) -> Fetched[list[SkinCard]]:
    """Calcula el ranking trending actual (sin cache, sin persistencia).

    Llamado por POST /internal/trending-tick; GET /market/trending lee el snapshot.
    Mismos estados que `compute_movers`.
    """
    now = time.monotonic()

    # ── Primary source: /items (paid plan) ───────────────────────────────────
    data, reason = await _ranking_items(client, "market-trending", "topmovers")
    if data is not None:
        catalog.cache_images(data)
        result = []
        for raw in data:
            if ranking_eligible(raw, MIN_SOLD_TRENDING):
                result.append(_map_item(raw))
        # Por relevancia (turnover = precio × unidades) y luego se reparte entre
        # categorías. Antes se ordenaba por categoría primero (`_category_rank`, ya
        # borrado), lo que agotaba "Rifle" antes de llegar a ninguna otra; y ordenar
        # por piezas vendidas premiaba lo barato y llenaba la lista de céntimos.
        result = _diversificar(
            sorted(result, key=_turnover, reverse=True), _TRENDING_CAPTURE_LIMIT
        )
        # Sin enrich_prices a propósito: es una llamada a csfloat/history
        # POR ITEM, el único coste que escala. Vive en enrich_trending
        # (/internal/enrich-tick), que rota sobre la tabla en pasadas de 18.
        # Hasta que a un item le toque la rueda, sus deltas son los de
        # _inline_delta (familia `pricereal`), que ya vienen en el payload.
        # enrich_market_prices sí se queda: son 2 peticiones fijas y
        # cacheadas, no escalan con el número de items.
        result = await pricing.enrich_market_prices(client, result)
        # steamwebapi /items no devuelve `image` en este plan → el cache estático
        # (ByMykel) es la única fuente. Igual que en /market/items (search).
        await catalog.fetch_static_images(client)
        catalog.enrich_images_from_cache(result)
        return Fetched(result)

    # ── Fallback: topmovers from cache (free plan) ────────────────────────────
    raw_topmovers = await _topmovers(client, "market-trending", now, log_sample=False)
    if raw_topmovers:
        gainers, losers = raw_topmovers
        combined = gainers + losers
        if combined:
            await catalog.fetch_static_images(client)
            result = [_map_topmovers_item(item) for item in combined]
            catalog.enrich_images_from_cache(result)
            result = sorted(result, key=lambda x: x["sold24h"], reverse=True)[:_TRENDING_FALLBACK_LIMIT]
            result = await pricing.enrich_market_prices(client, result)
            logger.info("[market-trending] serving from topmovers (%d items)", len(result))
            log_degraded("trending", reason or "unknown", "fallback")
            return Fetched(result, "partial", reason)

    logger.warning("[market-trending] no data available from any source")
    log_degraded("trending", reason or "unknown", "error")
    return Fetched([], "error", reason)


# ── Lecturas de /market ───────────────────────────────────────────────────────

async def get_movers() -> dict[str, list[RankedCard]]:
    rows = await movers_repo.fetch_snapshot()
    hot  = [_row_to_item(r) for r in rows if r.get("bucket") == "hot"]
    cold = [_row_to_item(r) for r in rows if r.get("bucket") == "cold"]
    return {"hot": hot, "cold": cold}


async def get_trending() -> list[RankedCard]:
    # Por turnover y no por `rank`: con upsert, un item que no aparece en una
    # captura conserva su rank viejo y ocuparía una posición alta como fantasma
    # hasta que la purga se lo lleve. El turnover se actualiza en cada captura.
    rows = await trending_repo.fetch_ranked()
    return [_row_to_item(row) for row in rows]


def _stale_or_raise(flow: str, stale: _T | None, exc: QuotaExhausted) -> Fetched[_T]:
    """SEC-16 — 402 de steamwebapi: mejor un dato caducado que un error, porque la
    cuota no vuelve hasta el día 10. Sin caché, el 402 sube y la ruta da 503."""
    if stale is not None:
        log_degraded(flow, "quota", "stale")
        return Fetched(stale, "stale", "quota")
    raise exc


async def search_market(client: httpx.AsyncClient, query: str) -> Fetched[list[SkinCard]]:
    """GET /market/items: búsqueda por nombre con el shape completo y caché de 5 min."""
    cache_key = query.lower()
    now = time.monotonic()
    hit = _search_cache.fresh(cache_key, now)
    if hit is not None:
        return Fetched(hit)

    try:
        data = await search_items(client, query, max=_SEARCH_LIMIT, select=_MOVERS_SELECT)
    except QuotaExhausted as exc:
        return _stale_or_raise("search", _search_cache.stale(cache_key), exc)
    if not isinstance(data, list):
        raise UnexpectedPayload(UNEXPECTED_FORMAT)

    catalog.cache_images(data)
    result = [
        _map_item(raw) for raw in data
        if float(raw.get("pricelatestsell") or 0) > 0
        and not is_sticker_slab(raw.get("marketname") or raw.get("market_hash_name") or "")
    ][:_SEARCH_LIMIT]

    await catalog.fetch_static_images(client)
    result = await pricing.enrich_market_prices(client, result)
    catalog.enrich_images_from_cache(result)

    _search_cache.put(cache_key, result, now)
    logger.info("[market-items] q=%r → %d results", query, len(result))
    return Fetched(result)


async def get_item_full(client: httpx.AsyncClient, query: str) -> Fetched[SkinCard | None]:
    """GET /market/price: un item con el shape completo de _map_item (liquidez y
    volumen incluidos), o None si la búsqueda no trae el nombre exacto."""
    cache_key = query.lower()
    now = time.monotonic()
    hit = _item_price_cache.fresh(cache_key, now)
    if hit is not None:
        return Fetched(hit)

    try:
        data = await search_items(client, query, max=_SEARCH_LIMIT, select=_MOVERS_SELECT)
    except QuotaExhausted as exc:
        return _stale_or_raise("item_price", _item_price_cache.stale(cache_key), exc)
    if not isinstance(data, list):
        raise UnexpectedPayload(UNEXPECTED_FORMAT)

    # steamwebapi /items?search es fuzzy: nos quedamos con el match exacto por
    # markethashname (el nombre canónico en inglés, el mismo que manda el frontend).
    raw = next(
        (r for r in data
         if (r.get("markethashname") or r.get("marketname") or "").lower() == cache_key),
        None,
    )
    if raw is None:
        return Fetched(None)

    catalog.cache_images([raw])
    item = _map_item(raw)
    (item,) = await pricing.enrich_prices(client, [item])
    (item,) = await pricing.enrich_market_prices(client, [item])
    await catalog.fetch_static_images(client)
    catalog.enrich_images_from_cache([item])

    _item_price_cache.put(cache_key, item, now)
    logger.info("[market-price] name=%r → hit", query)
    return Fetched(item)


async def get_market_index(client: httpx.AsyncClient, tf: str) -> Fetched[dict]:
    cache_key = tf
    now = time.monotonic()
    hit = _market_index_cache.fresh(cache_key, now)
    if hit is not None:
        return Fetched(hit)

    try:
        data = await steamwebapi.market_index(client)
    except QuotaExhausted as exc:
        logger.warning("[market-index] daily limit reached (402)")
        return _stale_or_raise("market_index", _market_index_cache.stale(cache_key), exc)
    except (SourceTimeout, SourceUnavailable):
        raise
    except UpstreamError as exc:
        logger.error("[market-index] steamwebapi returned %s | body: %s", exc.status, exc.body_excerpt[:500])
        raise

    if isinstance(data, list):
        raw_points = data
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
                raise UnexpectedPayload(UNEXPECTED_FORMAT)
        else:
            logger.error("[market-index] 'history' unexpected type: %s | sample: %s", type(history).__name__, str(history)[:200])
            raise UnexpectedPayload(UNEXPECTED_FORMAT)

        topmovers = data.get("topmovers", {})
        gainers = topmovers.get("gainers", [])
        losers  = topmovers.get("losers", [])
        top = gainers[0] if gainers else None
        if gainers:
            logger.info("[market-index] topmovers gainer keys: %s", list(gainers[0].keys()))
            logger.info("[market-index] topmovers gainer sample: %s", gainers[0])
        _topmovers_raw_cache.put("latest", (gainers, losers), now)
        turnover24h = float(data.get("turnover24h") or 0)
        sold24h = int(data.get("sold24h") or 0)
    else:
        logger.error("[market-index] unexpected top-level type: %s", type(data).__name__)
        raise UnexpectedPayload(UNEXPECTED_FORMAT)

    # UX-39: topmovers no trae la rareza; sale del catálogo estático (23 h, sin cuota).
    rarity = None
    if top:
        await catalog.fetch_static_images(client)
        rarity = catalog.rarity_from_cache(top["markethashname"])

    result = {
        "turnover24h": turnover24h,
        "sold24h": sold24h,
        # UX-35: no es «el más activo» sino el que más ha subido de precio en 24 h
        # (gainers[0]); change24h es ese porcentaje. El nombre del campo se conserva
        # por contrato con el front.
        "hottestItem": {
            "name": top["markethashname"] if top else "—",
            "change24h": float(top["change24h"]) if top else 0.0,
            # UX-38: el precio pone el porcentaje en contexto (+450 % de 0,17 $).
            "price": float(top["price"]) if top and top.get("price") is not None else None,
            "rarity": rarity[0] if rarity else None,
            "rarityColor": rarity[1] if rarity else None,
        },
        "history": [_map_market_index_point(p) for p in raw_points],
    }
    _market_index_cache.put(cache_key, result, now)
    return Fetched(result)


async def get_market_prices(client: httpx.AsyncClient, market: str, name: str | None,
                            currency: str | None) -> Fetched[Any]:
    """GET /market/prices: passthrough de /market/{market}/prices, caché de 5 min."""
    cache_key = f"{market}:{(name or '').lower()}:{(currency or 'usd').lower()}"
    now = time.monotonic()
    hit = _market_prices_cache.fresh(cache_key, now)
    if hit is not None:
        return Fetched(hit)

    params: dict = {}
    if name:
        params["market_hash_name"] = name
    if currency:
        params["currency"] = currency

    try:
        data = await steamwebapi.market_prices(client, market, params, timeout=15.0)
    except QuotaExhausted as exc:
        return _stale_or_raise("market_prices", _market_prices_cache.stale(cache_key), exc)

    _market_prices_cache.put(cache_key, data, now)
    logger.info("[market-prices] market=%r name=%r → %s items", market, name, len(data) if isinstance(data, list) else "object")
    return Fetched(data)


# ── Histórico del índice (cap-history) ────────────────────────────────────────

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


async def get_cap_history(tf: str) -> list[dict]:
    """Serie del índice desde Supabase, agrupada según `tf` (validado por la ruta)."""
    cutoff = datetime.now(timezone.utc) - _CAP_TF_MAP[tf]
    rows = await fetch_range(cutoff)
    return _downsample(rows, _CAP_BUCKET_MAP[tf])


# ── Ticks (crons externos) ────────────────────────────────────────────────────

async def capture_cap_snapshot(client: httpx.AsyncClient) -> dict:
    """/internal/cap-tick: guarda un snapshot horario del índice de precio."""
    try:
        data = await steamwebapi.market_index(client, timeout=15.0)
    except (SourceTimeout, SourceUnavailable):
        raise
    except UpstreamError as exc:
        logger.warning("[cap-tick] market-index returned %s", exc.status)
        raise

    if not isinstance(data, dict):
        raise UnexpectedPayload(UNEXPECTED_FORMAT)

    price_index = data.get("priceindex")
    if price_index is None:
        logger.warning("[cap-tick] 'priceindex' missing from response")
        raise UnexpectedPayload("'priceindex' missing from Steam response")

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


async def capture_trending(client: httpx.AsyncClient) -> dict:
    """/internal/trending-tick: captura el ranking trending en `market_trending`."""
    items = (await compute_trending(client)).data
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


async def enrich_trending(client: httpx.AsyncClient) -> dict:
    """/internal/enrich-tick: avanza la rueda de enriquecimiento. Coge los N items
    menos-recientemente enriquecidos y les recalcula los deltas desde el histórico
    de csfloat.

    Es el único coste que escala con el número de items (1 req por item), por
    eso está separado de la captura y capado a _ENRICH_BATCH por pasada.
    """
    names = await trending_repo.fetch_stalest(_ENRICH_BATCH)
    if not names:
        return {"ok": True, "count": 0, "with_deltas": 0}

    # enrich_prices solo lee item["name"] y sobrescribe los tres deltas, así
    # que no hace falta cargar la fila entera de Supabase.
    stubs = [{"name": n, "priceDelta24h": None, "priceDelta7d": None, "priceDelta30d": None}
             for n in names]
    enriched = await pricing.enrich_prices(client, stubs)

    enriched_at = datetime.now(timezone.utc).isoformat()
    con_deltas: list[dict] = []
    sin_deltas: list[dict] = []
    for e in enriched:
        # Si csfloat no devolvió histórico, enrich_prices deja el stub intacto
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


async def capture_movers(client: httpx.AsyncClient) -> dict:
    """/internal/movers-tick: captura el ranking hot/cold en `market_movers`."""
    result = (await compute_movers(client)).data
    # list[Any]: los repos de Supabase tipan dict y RankingRow es un TypedDict.
    rows: list[Any] = [_to_row(item, rank, "hot")  for rank, item in enumerate(result["hot"])] \
         + [_to_row(item, rank, "cold") for rank, item in enumerate(result["cold"])]
    # CAL-10: sin filas es que no respondió ninguna fuente. Un replace-all vaciaría la
    # tabla y la Home se quedaría sin hot/cold: se conserva el snapshot anterior.
    if not rows:
        logger.warning("[movers-tick] sin datos de ninguna fuente; se conserva el snapshot anterior")
        return {"ok": False, "count": 0, "kept_previous": True}
    await movers_repo.replace_snapshot(rows)
    logger.info("[movers-tick] snapshot saved: %d items", len(rows))
    return {"ok": True, "count": len(rows)}
