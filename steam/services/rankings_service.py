"""Rankings de mercado: los de Home (cajas más vendidas y skins que suben/bajan, en
`market_movers`) y trending (desde /items con respaldo de topmovers), su lectura desde los
snapshots de Supabase y los ticks que los persisten (capture_trending, enrich_trending,
capture_movers). Sin FastAPI.
"""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from settings import TRENDING_TRACK_TOP
from steam.cache.history_cache import _topmovers_raw_cache
from steam.adapters.steam_adapter import adapt_items, adapt_market_index
from steam.api import steam_client
from steam.errors.handling import degraded, log_degraded, reason_of
from steam.domain import home_rankings
from steam.domain.models import Fetched, MoverCard, RankedCard, SkinCard, SteamItem, TopMover
from steam.domain.rules import (
    MIN_SOLD_TRENDING, diversificar, is_sticker_slab, ranking_eligible, turnover,
)
from steam.errors import (
    InvalidPayload, SourceTimeout, SourceUnavailable, StorageError, UnexpectedPayload, UpstreamError,
)
from steam.mappers.item_mapper import _map_item
from steam.mappers.movers_mapper import _map_topmovers_item
from steam.mappers.row_mapper import _row_to_item, _to_row
from steam.rankings_repo import movers_repo, trending_repo
from steam.services import catalog_service, pricing_service

logger = logging.getLogger("uvicorn.error")

_MOVERS_SELECT = ",".join([
    "id", "marketname", "markethashname", "slug", "image",
    "pricelatestsell",
    # Mediana de Steam: el precio de las tarjetas de Home (una venta suelta no es un precio).
    "pricemedian",
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
# relevancia (`rules.turnover` = precio × unidades): viven en domain/rules.py.


async def _ranking_items(client: httpx.AsyncClient, tag: str, fallback_label: str,
                         price_real_min: float | None = None) -> tuple[list[SteamItem] | None, str | None]:
    """Fuente principal de movers y trending: /items por unidades vendidas. Devuelve
    (items, None), o (None, motivo) si no responde, no es JSON o no es una lista (un 200
    ilegible es una degradación, no un 500: CAL-14); entonces se cae a topmovers."""
    try:
        data = await steam_client.items(
            client, sort_by="soldZa", max=_ITEMS_FETCH_MAX, select=_MOVERS_SELECT,
            price_real_min=price_real_min,
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

    Solo vale lo cacheado dentro de `TOPMOVERS_RAW_TTL` (CAL-12: un ranking de hace días
    no es respaldo); si caducó o no hay, se pide market-index ahora. Si eso falla y lo que había estaba caducado, el motivo es `topmovers_stale`.
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


def _empty_home() -> dict[str, list[MoverCard]]:
    return {"cases": [], "up": [], "down": []}


def _home_card(item: SteamItem, change: tuple[float, float] | None) -> MoverCard:
    """Tarjeta de Home: precio = mediana de Steam, y en suben/bajan el % sin redondear
    (de él se deriva el cambio en $ al leer el snapshot) y el cambio en $."""
    card: Any = {**_map_item(item), "priceLatest": item.price_median, "priceChange30d": None}
    if change is not None:
        card["priceChange30d"], card["priceDelta30d"] = change
    return card


async def compute_home_rankings(client: httpx.AsyncClient) -> Fetched[dict[str, list[MoverCard]]]:
    """Cajas más vendidas y skins que más suben/bajan en $ a 30 días (sin cache ni persistencia).

    Llamado por POST /internal/movers-tick; GET /market/movers lee el snapshot. Dos peticiones a
    /items: la de siempre (por unidades) y la de las caras (precio real ≥ 50 $), que la primera
    no trae. Sin respaldo de topmovers (da % en 24 h, no encaja): si falla la primera, `error`
    y el tick conserva el snapshot anterior; si falla solo la de las caras, `partial`.
    """
    cheap, reason = await _ranking_items(client, "market-movers", "keeping previous snapshot")
    if cheap is None:
        return degraded("movers", reason or "unknown", "error", _empty_home())
    expensive, expensive_reason = await _ranking_items(
        client, "market-movers", "cheap items only",
        price_real_min=home_rankings.PRICE_REAL_MIN_EXPENSIVE,
    )
    universe = [i for i in home_rankings.merge_universe(cheap, expensive or [])
                if not is_sticker_slab(i)]
    catalog_service.cache_images(universe)
    result: dict[str, list[MoverCard]] = {
        "cases": [_home_card(i, None) for i in home_rankings.top_cases(universe)],
        "up":    [_home_card(i, (usd, pct)) for i, usd, pct in home_rankings.top_movers(universe, "up")],
        "down":  [_home_card(i, (usd, pct)) for i, usd, pct in home_rankings.top_movers(universe, "down")],
    }
    # steamwebapi /items no devuelve `image` en este plan → el cache estático (ByMykel).
    await catalog_service.fetch_static_images(client)
    for cards in result.values():
        catalog_service.enrich_images_from_cache(cards)
        await pricing_service.enrich_market_prices(client, cards)
    logger.info("[market-movers] cases=%d up=%d down=%d (universe=%d)",
                len(result["cases"]), len(result["up"]), len(result["down"]), len(universe))
    if expensive is None:
        return degraded("movers", expensive_reason or "unknown", "fallback", result)
    return Fetched(result)


async def compute_trending(client: httpx.AsyncClient) -> Fetched[list[SkinCard]]:
    """Calcula el ranking trending actual (sin cache, sin persistencia).

    Llamado por POST /internal/trending-tick; GET /market/trending lee el snapshot.
    `partial` = respaldo de topmovers; `error` = ninguna fuente (lista vacía).
    """
    now = time.monotonic()

    # ── Primary source: /items (paid plan) ───────────────────────────────────
    data, reason = await _ranking_items(client, "market-trending", "topmovers")
    if data is not None:
        catalog_service.cache_images(data)
        result = []
        for item in data:
            # Slabs fuera también aquí, como en movers y búsqueda: no son skins (CAL-14).
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
            result = sorted(result, key=lambda x: x["sold24h"] or 0, reverse=True)[:_TRENDING_FALLBACK_LIMIT]
            result = await pricing_service.enrich_market_prices(client, result)
            logger.info("[market-trending] serving from topmovers (%d items)", len(result))
            return degraded("trending", reason or "unknown", "fallback", result)

    logger.warning("[market-trending] no data available from any source")
    return degraded("trending", reason or "unknown", "error", [])


# ── Lecturas de /market ───────────────────────────────────────────────────────

# UX-46 (opción A): la lectura no sabe cómo le fue al tick, solo cuándo escribió. Tres
# ticks perdidos seguidos marcan el snapshot como viejo; uno suelto es ruido del cron.
_MOVERS_STALE_AFTER = timedelta(minutes=45)     # movers-tick cada 15 min
_TRENDING_STALE_AFTER = timedelta(hours=3)      # trending-tick cada hora


@dataclass(frozen=True)
class RankingSnapshot:
    """El ranking leído de Supabase y la fecha de su última escritura, si la hay."""
    fetched: Fetched[Any]
    captured_at: str | None


def _parse_ts(value: Any) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _snapshot(flow: str, rows: list[dict], column: str, max_age: timedelta,
              data: Any) -> RankingSnapshot:
    """`updated_at` (movers, replace-all) o `seen_at` (trending, upsert) de la fila más
    reciente decide si el snapshot es viejo. Sin filas: `error`, la pantalla sale vacía."""
    if not rows:
        return RankingSnapshot(degraded(flow, "empty_snapshot", "empty", data), None)
    stamps = [(ts, r[column]) for r in rows if (ts := _parse_ts(r.get(column))) is not None]
    if not stamps:
        return RankingSnapshot(Fetched(data), None)
    newest, captured_at = max(stamps, key=lambda s: s[0])
    if datetime.now(timezone.utc) - newest > max_age:
        return RankingSnapshot(degraded(flow, "snapshot_age", "stale", data), captured_at)
    return RankingSnapshot(Fetched(data), captured_at)


def _bucket(rows: list[dict], bucket: str) -> list[RankedCard]:
    return [_row_to_item(r) for r in rows if r.get("bucket") == bucket]


async def get_movers() -> RankingSnapshot:
    """Rankings de Home. Suben/bajan llevan el cambio a 30 días en $, derivado del % que
    se guarda sin redondear. Filas de otro `bucket` (el HOT/COLD de antes del deploy) no
    entran: hasta el primer movers-tick las listas salen vacías."""
    rows = await movers_repo.fetch_snapshot()
    data: dict[str, list[Any]] = {"cases": _bucket(rows, "cases")}
    for bucket in ("up", "down"):
        data[bucket] = [
            {**card, "priceChange30d": home_rankings.usd_from_pct(card["priceReal"], card["priceDelta30d"])}
            for card in _bucket(rows, bucket)
        ]
    # Transición: las apps ya instaladas leen `hot`/`cold`; sin alias su Home quedaría vacía.
    # Quitar cuando no queden builds anteriores a los rankings de Home en uso.
    data["hot"], data["cold"] = data["up"], data["down"]
    return _snapshot("movers_snapshot", rows, "updated_at", _MOVERS_STALE_AFTER, data)


async def get_trending() -> RankingSnapshot:
    # Por turnover y no por `rank`: con upsert, un item que no aparece en una
    # captura conserva su rank viejo y ocuparía una posición alta como fantasma
    # hasta que la purga se lo lleve. El turnover se actualiza en cada captura.
    rows = await trending_repo.fetch_ranked()
    items: list[RankedCard] = [_row_to_item(row) for row in rows]
    return _snapshot("trending_snapshot", rows, "seen_at", _TRENDING_STALE_AFTER, items)


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
    """/internal/movers-tick: captura los rankings de Home en `market_movers`
    (`bucket` = cases | up | down)."""
    fetched = await compute_home_rankings(client)
    result = fetched.data
    # list[Any]: los repos de Supabase tipan dict y RankingRow es un TypedDict.
    rows: list[Any] = [_to_row(item, rank, bucket)
                       for bucket, cards in result.items() for rank, item in enumerate(cards)]
    if fetched.status == "partial":
        # Falló solo la petición de las caras: suben/bajan saldrían sin cuchillos ni guantes
        # y sin aviso (GET solo ve la edad del snapshot). Se renuevan las cajas y se conservan
        # las listas anteriores, que sí tenían las caras.
        previous = [r for r in await movers_repo.fetch_snapshot() if r.get("bucket") in ("up", "down")]
        if previous:
            rows = [r for r in rows if r["bucket"] == "cases"] + previous
    # CAL-10: sin filas es que no respondió ninguna fuente. Un replace-all vaciaría la
    # tabla y la Home se quedaría sin rankings: se conserva el snapshot anterior.
    if not rows:
        logger.warning("[movers-tick] sin datos de ninguna fuente; se conserva el snapshot anterior")
        return {"ok": False, "count": 0, "kept_previous": True}
    await movers_repo.replace_snapshot(rows)
    logger.info("[movers-tick] snapshot saved: %d items", len(rows))
    if fetched.status == "partial":
        # Que el cron y los logs lo vean: el workflow solo mira `ok`.
        return {"ok": True, "count": len(rows), "status": fetched.status, "reason": fetched.reason}
    return {"ok": True, "count": len(rows)}
