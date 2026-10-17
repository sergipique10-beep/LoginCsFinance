"""Rankings, índice y cierre diario propios (COST-05). Sustituyen a los ticks de Render
(trending-tick, movers-tick, cap-tick, price-tick) sin steamwebapi.

  compute_rankings   6,21,36,51   referencia + catálogo + ventas → tarjetas → trending (top-500 por
                                  turnover, diversificado) y movers (hot/cold por delta 7 d) →
                                  Supabase MARKET_TRENDING_TABLE / MARKET_MOVERS_TABLE (sombra)
  compute_index      :10          índice encadenado (worker/pricing/index.py) → MARKET_CAP_HISTORY_TABLE
  daily_close        00:05        cierre de ayer de la serie propia para las tracked_skins →
                                  PRECIOS_HISTORICOS_TABLE (source='own')

Las tarjetas salen de la MISMA lectura que market_current (market_sync.compute_references), no de la
tabla de Supabase: mismos números, sin paginar 47 k filas por PostgREST. Las reglas (elegibilidad,
turnover, diversificación, slabs) y la fila (`_to_row`) son las del backend: mismo JSON que hoy. El
worker no escribe `liquidity_breakdown` (invariante del detail sheet). Nada de aquí toca Steam.
"""
import datetime as dt
import json
import logging
from typing import Any

from steam.domain.rules import MIN_SOLD_MOVERS, MIN_SOLD_TRENDING, diversificar, is_sticker_slab, ranking_eligible, turnover
from steam.mappers.row_mapper import _to_row
from worker import settings
from worker.adapters import own_card
from worker.broker import broker
from worker.jobs import market_sync as ms
from worker.pricing import index as ix
from worker.store import market
from worker.store import supabase as sb_store

log = logging.getLogger(__name__)

TRENDING_LIMIT = 500          # = rankings_service._TRENDING_CAPTURE_LIMIT
MOVERS_LIMIT = 10             # = movers_mapper._MOVERS_LIMIT (hot y cold)
TRENDING_STALE_DAYS = 7       # = rankings_service._TRENDING_STALE_DAYS
STEAM_FRESH = dt.timedelta(hours=2)   # una lectura de Steam más vieja no sustituye a Skinport en el índice
INDEX_STATE_KEY = "index:state"

Card = dict[str, Any]


# ---------------------------------------------------------------- puro

def eligible(cards: list[Card], min_sold: int) -> list[Card]:
    out = []
    for c in cards:
        item = own_card.steam_item(c)
        if ranking_eligible(item, min_sold) and not is_sticker_slab(item):
            out.append(c)
    return sorted(out, key=turnover, reverse=True)


def compute_trending(cards: list[Card]) -> list[Card]:
    """Top-500 por turnover, repartido entre categorías (igual que rankings_service.compute_trending)."""
    return diversificar(eligible(cards, MIN_SOLD_TRENDING), TRENDING_LIMIT)


def compute_movers(cards: list[Card]) -> dict[str, list[Card]]:
    """Hot/cold por delta 7 d entre los 20 candidatos de más turnover (igual que compute_movers)."""
    candidates = diversificar(eligible(cards, MIN_SOLD_MOVERS), MOVERS_LIMIT * 2)
    with_delta = sorted([c for c in candidates if c["priceDelta7d"] is not None], key=lambda c: c["priceDelta7d"])
    no_delta = [c for c in candidates if c["priceDelta7d"] is None]
    hot = list(reversed(with_delta[-MOVERS_LIMIT:])) + no_delta
    cold = with_delta[:MOVERS_LIMIT] + no_delta
    return {"hot": hot[:MOVERS_LIMIT], "cold": cold[:MOVERS_LIMIT]}


def steam_fresh(refs: list[Any], now: dt.datetime) -> dict[str, tuple[float, int]]:
    """(precio, ventas 24 h) de Steam por nombre, solo con lectura ≤ STEAM_FRESH (N pequeño de COST-04)."""
    out = {}
    for r in refs:
        for p in r.prices:
            if p["market"] == "steam" and p["price"] and p["volume"] is not None \
                    and now - dt.datetime.fromisoformat(p["ts"]) <= STEAM_FRESH:
                out[r.market_hash_name] = (p["price"], p["volume"])
    return out


# ---------------------------------------------------------------- lecturas

async def load_stats(redis: Any) -> dict[str, ix.SalesStats]:
    raw = await redis.hgetall(ms.SALES_KEY)
    return {(k.decode() if isinstance(k, bytes) else k): ix.SalesStats(**json.loads(v)) for k, v in raw.items()}


async def build_cards(state: Any, now: dt.datetime) -> tuple[list[Card], dict[str, ix.SalesStats], list[Any]]:
    refs = await ms.compute_references(state, now)
    cat = await market.catalog_all(state.pg)
    if not cat:
        # Sin catálogo no hay categoría ni imagen (y `diversificar` no reparte). El job semanal tarda hasta el
        # lunes: la primera vez se trae aquí.
        await ms.run_catalog(state)
        cat = await market.catalog_all(state.pg)
    stats = await load_stats(state.redis)
    today = now.date()
    closes = await market.daily_closes(state.pg, [today - dt.timedelta(days=d) for d in (1, 7, 30)])
    cards = []
    for r in refs:
        if not r.ref_price_usd:
            continue
        c = closes.get(r.market_hash_name, {})
        cl = own_card.Closes(*(c.get(today - dt.timedelta(days=d)) for d in (1, 7, 30)))
        cards.append(own_card.card(r, cat.get(r.market_hash_name), stats.get(r.market_hash_name), cl))
    return cards, stats, refs


# ---------------------------------------------------------------- jobs

async def run_compute_rankings(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        now = ms.now_utc()
        cards, _, _ = await build_cards(state, now)
        trending = compute_trending(cards)
        movers = compute_movers(cards)
        stamp = now.isoformat()
        rows: list[dict[str, Any]] = [{**_to_row(c, i), "seen_at": stamp} for i, c in enumerate(trending)]
        mrows: list[dict[str, Any]] = [dict(_to_row(c, i, "hot")) for i, c in enumerate(movers["hot"])] \
            + [dict(_to_row(c, i, "cold")) for i, c in enumerate(movers["cold"])]
        if rows:
            await sb_store.upsert(state.supabase, settings.MARKET_TRENDING_TABLE, rows, "name")
            await sb_store.delete_older(state.supabase, settings.MARKET_TRENDING_TABLE, "seen_at",
                                        (now - dt.timedelta(days=TRENDING_STALE_DAYS)).isoformat())
        if mrows:   # sin filas se conserva el snapshot anterior (CAL-10)
            await sb_store.replace_all(state.supabase, settings.MARKET_MOVERS_TABLE, mrows)
        return {"cards": len(cards), "trending": len(rows), "hot": len(movers["hot"]), "cold": len(movers["cold"]),
                "with_delta7d": sum(1 for c in trending if c["priceDelta7d"] is not None)}
    return await ms._run(state, "compute_rankings", go)


async def run_compute_index(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        now = ms.now_utc()
        stats = await load_stats(state.redis)
        if not stats:
            raise RuntimeError("sin estadísticas de ventas (skinport_sales aún no ha corrido)")
        refs = await ms.compute_references(state, now)
        price = {r.market_hash_name: r.ref_price_usd for r in refs if r.ref_price_usd}
        real = {n: s.median_24h for n, s in stats.items() if s.median_24h}
        raw = await state.redis.get(INDEX_STATE_KEY)
        st = ix.IndexState.from_json(json.loads(raw) if raw else None)
        i0_price, i0_real = 0.0, None
        if st.priceindex is None:
            # I_0: la serie propia continúa donde acaba la de steamwebapi.
            last = await sb_store.last_cap_point(state.supabase)
            if not last or last.get("priceindex") is None:
                raise RuntimeError("sin I_0: market_cap_history de steamwebapi vacío")
            i0_price, i0_real = float(last["priceindex"]), last.get("realpriceindex")
        st, used = ix.step(st, now.strftime("%Y-%m"), stats, price, real, i0_price, i0_real)
        await state.redis.set(INDEX_STATE_KEY, json.dumps(st.to_json()))
        turnover24h, sold24h = ix.turnover_24h(stats, steam_fresh(refs, now))
        point = {"ts": now.replace(minute=0, second=0, microsecond=0).isoformat(), "priceindex": st.priceindex,
                 "realpriceindex": st.realpriceindex, "buyorderpriceindex": None,
                 "turnover24h": turnover24h, "sold24h": sold24h}
        await sb_store.upsert(state.supabase, settings.MARKET_CAP_HISTORY_TABLE, [point], "ts")
        return {"priceindex": round(st.priceindex or 0, 2), "basket": len(st.weights), "chained": used,
                "turnover24h": turnover24h}
    return await ms._run(state, "compute_index", go)


async def run_daily_close(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        day = (ms.now_utc() - dt.timedelta(days=1)).date()
        closes = await market.daily_closes(state.pg, [day])
        volume = await market.daily_volume(state.pg, day)
        tracked = {r["market_hash_name"] for r in await sb_store.select_all(state.supabase, "tracked_skins",
                                                                             "market_hash_name")}
        rows = [{"market_hash_name": n, "date": day.isoformat(), "price": round(c[day], 4),
                 "volume": volume.get(n), "source": "own"}
                for n, c in closes.items() if n in tracked and day in c]
        if rows:
            await sb_store.upsert(state.supabase, settings.PRECIOS_HISTORICOS_TABLE, rows, "market_hash_name,date")
        return {"date": day.isoformat(), "tracked": len(tracked), "rows": len(rows)}
    return await ms._run(state, "daily_close", go)


@broker.task(task_name="compute_rankings", schedule=[{"cron": "6,21,36,51 * * * *"}])
async def compute_rankings(context: ms.Ctx) -> dict[str, Any]:
    return await run_compute_rankings(context.state)


@broker.task(task_name="compute_index", schedule=[{"cron": "10 * * * *"}])
async def compute_index(context: ms.Ctx) -> dict[str, Any]:
    return await run_compute_index(context.state)


@broker.task(task_name="daily_close", schedule=[{"cron": "5 0 * * *"}])
async def daily_close(context: ms.Ctx) -> dict[str, Any]:
    return await run_daily_close(context.state)
