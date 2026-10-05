"""Rankings de mercado (CLEAN-18, ex market_service): hot/cold (movers) y trending desde
/items con respaldo de topmovers, su lectura desde los snapshots de Supabase y los ticks
que los persisten (capture_trending, enrich_trending, capture_movers). Sin FastAPI.
"""
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import httpx

from settings import TRENDING_TRACK_TOP
from steam.cache.history_cache import _topmovers_raw_cache
from steam.adapters.steam_adapter import adapt_items, adapt_market_index
from steam.api import steam_client
from steam.errors.handling import degraded, log_degraded, reason_of
from steam.domain.models import Fetched, MoverItem, RankedCard, SkinCard, SteamItem, TopMover
from steam.domain.rules import (
    MIN_SOLD_MOVERS, MIN_SOLD_TRENDING, diversificar, is_sticker_slab, ranking_eligible, turnover,
)
from steam.errors import (
    InvalidPayload, SourceTimeout, SourceUnavailable, StorageError, UnexpectedPayload, UpstreamError,
)
from steam.mappers.item_mapper import _map_item
from steam.mappers.movers_mapper import _MOVERS_LIMIT, _map_topmovers_item
from steam.mappers.row_mapper import _row_to_item, _to_row
from steam.rankings_repo import movers_repo, trending_repo
from steam.services import catalog_service, pricing_service

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

# Cuotas por categoría y por skin del reparto (`rules.diversificar`), y el criterio de
# relevancia (`rules.turnover` = precio × unidades): viven en domain/rules.py (CLEAN-17).


async def _ranking_items(client: httpx.AsyncClient, tag: str,
                         fallback_label: str) -> tuple[list[SteamItem] | None, str | None]:
    """Fuente principal de movers y trending: /items por unidades vendidas. Devuelve
    (items, None), o (None, motivo) si no responde, no es JSON (CAL-14: antes 500) o no
    es una lista; entonces se cae a topmovers."""
    try:
        data = await steam_client.items(
            client, sort_by="soldZa", max=_ITEMS_FETCH_MAX, select=_MOVERS_SELECT,
        )
    except (SourceTimeout, SourceUnavailable, InvalidPayload) as exc:
        logger.warning("[%s] /items failed (%s) — falling back to %s", tag, reason_of(exc), fallback_label)
        return None, reason_of(exc)
    except UpstreamError as exc:
        logger.warning("[%s] /items returned %s — falling back to %s", tag, exc.status, fallback_label)
        return None, reason_of(exc)
    try:
        return adapt_items(data), None
    except UnexpectedPayload as exc:
        logger.warning("[%s] /items returned unexpected payload: %s", tag, exc)
        return None, "unexpected_format"


async def _topmovers(client: httpx.AsyncClient, tag: str,
                     now: float) -> tuple[tuple[tuple[TopMover, ...], tuple[TopMover, ...]] | None, str | None]:
    """Respaldo: topmovers de market-index. Devuelve (gainers+losers, None), o (None,
    motivo) si no hay nada que servir.

    Solo vale lo cacheado dentro de `TOPMOVERS_RAW_TTL` (CAL-12: antes se usaba lo último
    que hubiera, tuviera la edad que tuviera); si caducó o no hay, se pide market-index
    ahora. Si eso falla y lo que había estaba caducado, el motivo es `topmovers_stale`.
    """
    cached = _topmovers_raw_cache.fresh("latest", now)
    if cached:
        return cached, None
    try:
        mi = adapt_market_index(await steam_client.market_index(client, timeout=15.0))
    except (UpstreamError, InvalidPayload, UnexpectedPayload) as exc:
        logger.warning("[%s] could not fetch market-index for topmovers: %s", tag, reason_of(exc))
        log_degraded("topmovers", reason_of(exc), "empty")
        expired = _topmovers_raw_cache.stale("latest")
        return None, "topmovers_stale" if expired else None
    _topmovers_raw_cache.put("latest", (mi.gainers, mi.losers), now)
    return (mi.gainers, mi.losers), None


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
        catalog_service.cache_images(data)
        mapped = []
        for item in data:
            if ranking_eligible(item, MIN_SOLD_MOVERS) and not is_sticker_slab(item):
                mapped.append(_map_item(item))
        # Ordenar por turnover (precio × unidades), no por precio suelto: mide
        # qué mueve dinero de verdad. Cap at 20 (= _MOVERS_LIMIT * 2): exactly
        # the number of items displayed, and safe within the Starter plan's
        # 20 req/min rate limit.
        mapped.sort(key=turnover, reverse=True)
        # Diversificar ANTES de enriquecer: enrich_prices gasta una llamada
        # por item (limiter 18/60s), así que descartar después sería tirar
        # cuota en items que no se van a mostrar.
        # list[Any]: enrich_prices devuelve dicts nuevos sin tipar.
        candidates: list[Any] = diversificar(mapped, _MOVERS_LIMIT * 2)
        logger.info("[market-movers] candidates: %d (capped from %d)", len(candidates), len(mapped))
        candidates = await pricing_service.enrich_prices(client, candidates)
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
        await catalog_service.fetch_static_images(client)
        catalog_service.enrich_images_from_cache(result["hot"])
        catalog_service.enrich_images_from_cache(result["cold"])
        await pricing_service.enrich_market_prices(client, result["hot"])
        await pricing_service.enrich_market_prices(client, result["cold"])
        return Fetched(result)

    # ── Fallback: market-index topmovers (free plan) ─────────────────────────
    raw_topmovers, topmovers_reason = await _topmovers(client, "market-movers", now)
    reason = topmovers_reason or reason
    if raw_topmovers:
        gainers, losers = raw_topmovers
        fallback = _build_movers_from_topmovers(gainers, losers)
        if fallback:
            await catalog_service.fetch_static_images(client)
            catalog_service.enrich_images_from_cache(fallback["hot"])
            catalog_service.enrich_images_from_cache(fallback["cold"])
            await pricing_service.enrich_market_prices(client, fallback["hot"])
            await pricing_service.enrich_market_prices(client, fallback["cold"])
            logger.info("[market-movers] serving from market-index topmovers (%d hot, %d cold)", len(fallback["hot"]), len(fallback["cold"]))
            return degraded("movers", reason or "unknown", "fallback", fallback)

    logger.warning("[market-movers] no data available from any source")
    return degraded("movers", reason or "unknown", "error", {"hot": [], "cold": []})


async def compute_trending(client: httpx.AsyncClient) -> Fetched[list[SkinCard]]:
    """Calcula el ranking trending actual (sin cache, sin persistencia).

    Llamado por POST /internal/trending-tick; GET /market/trending lee el snapshot.
    Mismos estados que `compute_movers`.
    """
    now = time.monotonic()

    # ── Primary source: /items (paid plan) ───────────────────────────────────
    data, reason = await _ranking_items(client, "market-trending", "topmovers")
    if data is not None:
        catalog_service.cache_images(data)
        result = []
        for item in data:
            # Slabs fuera también aquí (CAL-14, CLEAN-17): antes solo los filtraban
            # movers y búsqueda, y el trending los colaba como si fueran skins.
            if ranking_eligible(item, MIN_SOLD_TRENDING) and not is_sticker_slab(item):
                result.append(_map_item(item))
        # Por relevancia (turnover = precio × unidades) y luego se reparte entre
        # categorías. Antes se ordenaba por categoría primero (`_category_rank`, ya
        # borrado), lo que agotaba "Rifle" antes de llegar a ninguna otra; y ordenar
        # por piezas vendidas premiaba lo barato y llenaba la lista de céntimos.
        result = diversificar(
            sorted(result, key=turnover, reverse=True), _TRENDING_CAPTURE_LIMIT
        )
        # Sin enrich_prices a propósito: es una llamada a csfloat/history
        # POR ITEM, el único coste que escala. Vive en enrich_trending
        # (/internal/enrich-tick), que rota sobre la tabla en pasadas de 18.
        # Hasta que a un item le toque la rueda, sus deltas son los de
        # _inline_delta (familia `pricereal`), que ya vienen en el payload.
        # enrich_market_prices sí se queda: son 2 peticiones fijas y
        # cacheadas, no escalan con el número de items.
        result = await pricing_service.enrich_market_prices(client, result)
        # steamwebapi /items no devuelve `image` en este plan → el cache estático
        # (ByMykel) es la única fuente. Igual que en /market/items (search).
        await catalog_service.fetch_static_images(client)
        catalog_service.enrich_images_from_cache(result)
        return Fetched(result)

    # ── Fallback: topmovers from cache (free plan) ────────────────────────────
    raw_topmovers, topmovers_reason = await _topmovers(client, "market-trending", now)
    reason = topmovers_reason or reason
    if raw_topmovers:
        gainers, losers = raw_topmovers
        combined = (*gainers, *losers)
        if combined:
            await catalog_service.fetch_static_images(client)
            result = [_map_topmovers_item(mover) for mover in combined]
            catalog_service.enrich_images_from_cache(result)
            result = sorted(result, key=lambda x: x["sold24h"], reverse=True)[:_TRENDING_FALLBACK_LIMIT]
            result = await pricing_service.enrich_market_prices(client, result)
            logger.info("[market-trending] serving from topmovers (%d items)", len(result))
            return degraded("trending", reason or "unknown", "fallback", result)

    logger.warning("[market-trending] no data available from any source")
    return degraded("trending", reason or "unknown", "error", [])


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


# ── Respaldo: ranking hot/cold desde los topmovers de market-index ─────────────

def _build_movers_from_topmovers(gainers: Sequence[TopMover],
                                 losers: Sequence[TopMover]) -> dict[str, list[MoverItem]] | None:
    if not gainers and not losers:
        return None
    hot  = [_map_topmovers_item(g) for g in gainers if not is_sticker_slab(g.item)][:_MOVERS_LIMIT]
    cold = [_map_topmovers_item(m) for m in losers  if not is_sticker_slab(m.item)][:_MOVERS_LIMIT]
    logger.info("[market-movers] topmovers raw: gainers=%d losers=%d | after_filter: hot=%d cold=%d",
                len(gainers), len(losers), len(hot), len(cold))
    hot  = sorted(hot,  key=lambda x: x["_change24h"], reverse=True)
    cold = sorted(cold, key=lambda x: x["_change24h"])
    for item in hot + cold:
        del item["_change24h"]
    return {"hot": hot, "cold": cold}


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
    # por turnover (`items` ya viene ordenado así desde rules.diversificar) los mete
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
    except StorageError as exc:
        logger.warning("[trending-tick] register_tracked falló: %s", exc)
        log_degraded("tracked_register", "storage", "empty")

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
    pendientes = await trending_repo.fetch_stalest(_ENRICH_BATCH)
    if not pendientes:
        return {"ok": True, "count": 0, "with_deltas": 0}

    # enrich_prices solo lee item["name"] y sobrescribe los tres deltas, así
    # que no hace falta cargar la fila entera de Supabase.
    stubs = [{"name": n, "priceDelta24h": None, "priceDelta7d": None, "priceDelta30d": None}
             for n in pendientes]
    enriched = await pricing_service.enrich_prices(client, stubs)

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
    logger.info("[enrich-tick] intentados=%d con_deltas=%d", len(pendientes), len(con_deltas))
    return {"ok": True, "count": len(pendientes), "with_deltas": len(con_deltas)}


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
