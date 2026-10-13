"""market-sync (COST-03): fuentes gratuitas → market_quotes → precio de referencia → market_current.

Cadencias (cron UTC):
  skinport_items      */5      Skinport /v1/items
  skinport_sales      */15     Skinport /v1/sales/history
  csgotm_prices       */15     market.csgo prices/USD.json
  csgotrader_buff     :07      buff163.json con If-None-Match (necesita BUFF_CURRENCY)
  fx_daily            06:05    USD→CNY (solo se usa si Buff viene en CNY)
  catalog_weekly      lun 04:13  ByMykel → catalog_items
  compute_reference   2,17,32,47  referencia → market_ref + Supabase (solo filas cambiadas)

Cada job deja su fila en `source_health` y empuja a Kuma con el nombre del job. Un fallo se
registra y se informa; no se relanza (el siguiente tick lo reintenta).
"""
import datetime as dt
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from taskiq import Context, TaskiqDepends

from worker import health, settings
from worker.adapters import catalog as catalog_adapter
from worker.adapters import csgotm as csgotm_adapter
from worker.adapters import csgotrader as csgotrader_adapter
from worker.adapters import fx as fx_adapter
from worker.adapters import skinport as skinport_adapter
from worker.broker import broker
from worker.pricing import reference
from worker.sources import catalog, csgotm, csgotrader, fx, skinport
from worker.store import market
from worker.store import supabase as sb_store

log = logging.getLogger(__name__)

FX_KEY = "fx:usd_per_cny"
HASH_KEY = "mc:hash"
REF_WINDOW = dt.timedelta(hours=24)

Ctx = Annotated[Context, TaskiqDepends()]


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _run(state: Any, job: str, fn: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        res = await fn()
        await market.source_ok(state.pg, job)
        ok = True
    except Exception as e:  # noqa: BLE001 — se registra en source_health y Kuma; el siguiente tick reintenta
        log.warning("[market-sync] job=%s error=%r", job, e)
        res = {"error": repr(e)[:200]}
        ok = False
        try:
            await market.source_fail(state.pg, job, repr(e))
        except Exception as e2:  # noqa: BLE001 — sin BD no hay dónde anotarlo; Kuma lo verá
            log.warning("[market-sync] job=%s source_health no escrito: %r", job, e2)
    ms = 1000 * (time.monotonic() - t0)
    await health.push(state.http, job, ok, msg=json.dumps(res, default=str)[:200], ping_ms=ms)
    log.info("[market-sync] job=%s ok=%s %s ms=%.0f", job, ok, res, ms)
    return {"ok": ok, **res}


async def _store(state: Any, quotes: list[Any]) -> dict[str, Any]:
    return {"quotes": await market.insert_quotes(state.pg, quotes)}


async def run_skinport_items(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        return await _store(state, skinport_adapter.adapt_items(await skinport.items(state), now_utc()))
    return await _run(state, "skinport_items", go)


async def run_skinport_sales(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        return await _store(state, skinport_adapter.adapt_sales(await skinport.sales_history(state), now_utc()))
    return await _run(state, "skinport_sales", go)


async def run_csgotm(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        return await _store(state, csgotm_adapter.adapt_prices(await csgotm.prices(state), now_utc()))
    return await _run(state, "csgotm_prices", go)


async def run_csgotrader(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        cur = settings.BUFF_CURRENCY
        if cur not in ("USD", "CNY"):
            raise csgotrader_adapter.CurrencyUnresolved("BUFF_CURRENCY sin fijar (cifra de COST-00)")
        raw = await csgotrader.buff163(state)
        if raw is None:
            return {"quotes": 0, "not_modified": True}
        rate = await state.redis.get(FX_KEY)
        quotes = csgotrader_adapter.adapt_buff(raw, now_utc(), cur, float(rate) if rate else None)
        return await _store(state, quotes)
    return await _run(state, "csgotrader_buff", go)


async def run_fx(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        rate = fx_adapter.usd_per_cny(await fx.usd_cny(state))
        if rate is None:
            raise ValueError("tipo USD/CNY ausente o implausible")
        await state.redis.set(FX_KEY, rate)
        return {"usd_per_cny": rate}
    return await _run(state, "fx_daily", go)


async def run_catalog(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        items = catalog_adapter.adapt_catalog(await catalog.all_files(state))
        await market.upsert_catalog(state.pg, items)
        return {"items": len(items)}
    return await _run(state, "catalog_weekly", go)


def current_row(r: reference.Reference) -> dict[str, Any]:
    """Fila de market_current (docs/sql/market_current.sql), sin updated_at."""
    return {"market_hash_name": r.market_hash_name, "ref_price_usd": r.ref_price_usd,
            "ref_confidence": r.ref_confidence, "ref_ts": r.ref_ts.isoformat() if r.ref_ts else None,
            "sources": r.sources, "volume_24h": r.volume_24h, "turnover_24h": r.turnover_24h,
            "prices": r.prices}


def row_hash(row: dict[str, Any]) -> str:
    # Las marcas de tiempo de cada cotización cambian en cada captura aunque el precio no:
    # fuera del hash, o todas las filas «cambiarían» en cada tick.
    stable = {**row, "ref_ts": None,
              "prices": [{k: v for k, v in p.items() if k != "ts"} for p in row["prices"]]}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


async def changed_rows(redis: Any, rows: list[dict[str, Any]], chunk: int = 1000) -> list[tuple[dict[str, Any], str]]:
    out = []
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        old = await redis.hmget(HASH_KEY, [r["market_hash_name"] for r in part])
        for row, prev in zip(part, old, strict=True):
            h = row_hash(row)
            if (prev.decode() if isinstance(prev, bytes) else prev) != h:
                out.append((row, h))
    return out


async def run_compute_reference(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        now = now_utc()
        by_name = await market.latest_quotes(state.pg, now - REF_WINDOW)
        k = reference.market_k()
        refs = [reference.compute(name, qs, now, k) for name, qs in by_name.items()]
        await market.insert_refs(state.pg, now, refs)
        rows = [current_row(r) for r in refs]
        changed = await changed_rows(state.redis, rows)
        if changed and state.supabase is not None:
            stamp = now.isoformat()
            await sb_store.upsert(state.supabase, settings.MARKET_CURRENT_TABLE,
                                  [{**row, "updated_at": stamp} for row, _ in changed], "market_hash_name")
            await state.redis.hset(HASH_KEY, mapping={row["market_hash_name"]: h for row, h in changed})
        return {"names": len(rows), "changed": len(changed),
                "changed_pct": round(100 * len(changed) / len(rows), 1) if rows else 0.0,
                "confidence_ge2": sum(1 for r in refs if r.ref_confidence >= 2)}
    return await _run(state, "compute_reference", go)


@broker.task(task_name="skinport_items", schedule=[{"cron": "*/5 * * * *"}])
async def skinport_items(context: Ctx) -> dict[str, Any]:
    return await run_skinport_items(context.state)


@broker.task(task_name="skinport_sales", schedule=[{"cron": "*/15 * * * *"}])
async def skinport_sales(context: Ctx) -> dict[str, Any]:
    return await run_skinport_sales(context.state)


@broker.task(task_name="csgotm_prices", schedule=[{"cron": "*/15 * * * *"}])
async def csgotm_prices(context: Ctx) -> dict[str, Any]:
    return await run_csgotm(context.state)


@broker.task(task_name="csgotrader_buff", schedule=[{"cron": "7 * * * *"}])
async def csgotrader_buff(context: Ctx) -> dict[str, Any]:
    return await run_csgotrader(context.state)


@broker.task(task_name="fx_daily", schedule=[{"cron": "5 6 * * *"}])
async def fx_daily(context: Ctx) -> dict[str, Any]:
    return await run_fx(context.state)


@broker.task(task_name="catalog_weekly", schedule=[{"cron": "13 4 * * 1"}])
async def catalog_weekly(context: Ctx) -> dict[str, Any]:
    return await run_catalog(context.state)


@broker.task(task_name="compute_reference", schedule=[{"cron": "2,17,32,47 * * * *"}])
async def compute_reference(context: Ctx) -> dict[str, Any]:
    return await run_compute_reference(context.state)
