"""Steam N pequeño (COST-04): priceoverview solo de los ítems que importan, dentro de la cuota de Steam.

Dos jobs, no un proceso continuo (el scheduler ya marca el ritmo y Kuma vigila cada job):

  steam_topn            cada hora                      recalcula la lista → Valkey `steam:topn` (LIST)
  steam_priceoverview   STEAM_PRICEOVERVIEW_CRON       saca los siguientes PER_TICK nombres por rotación
                                                       (LMOVE), pide priceoverview por el gate y escribe
                                                       market_quotes(steam)

Lista = alertas activas ∪ inventarios recientes (≤ 7 d, Skinport ≥ 10 $), en ese orden y sin repetir,
tope STEAM_TOPN. Sin top por turnover (2026-10-17): Steam agrupa priceoverview por IP + Accept-Encoding
y da ~100–150 peticiones por cubo con bloqueos de horas; el volumen del resto del catálogo lo da Skinport.
Un tick gasta como mucho PER_TICK del presupuesto diario `budget:priceoverview` (gate.try_spend). Un 429
abre cooldown en el gate y el tick termina en error; con cooldown activo el tick no rota ni gasta: el
monitor de Kuma lo enseña hasta que expira. Ritmo y presupuesto: fórmula en worker/settings.py.
"""
import logging
from typing import Any

from worker import settings
from worker.adapters import steam_market as adapter
from worker.broker import broker
from worker.models import Quote
from worker.jobs.market_sync import Ctx, _run, now_utc
from worker.net import retry
from worker.net.breaker import RateLimited
from worker.net.gate import HostCoolingDown
from worker.sources import steam_market
from worker.store import market
from worker.store import supabase as sb_store

log = logging.getLogger(__name__)

TOPN_KEY = "steam:topn"
BUDGET_KIND = "priceoverview"
# Nombre que Steam no tiene en el Market (500 tras los reintentos): se recuerda 7 días y no se vuelve a
# pedir. Cada 500 cuenta para el breaker del host (5 → abierto 15 min, también para el inventario).
MISS_KEY = "steam:miss:{name}"
MISS_TTL_S = 7 * 24 * 3600


def merge_names(*groups: list[str], cap: int) -> list[str]:
    """Unión ordenada por prioridad de grupo, sin repetidos, con tope."""
    out: list[str] = []
    seen: set[str] = set()
    for g in groups:
        for n in g:
            if n and n not in seen:
                seen.add(n)
                out.append(n)
    return out[:cap]


async def topn_names(state: Any) -> list[str]:
    alerts = await sb_store.active_alert_names(state.supabase) if state.supabase is not None else []
    inv = await market.recent_inventory_names(state.pg, settings.STEAM_TOPN_MIN_PRICE) if state.pg is not None else []
    return merge_names(alerts, inv, cap=settings.STEAM_TOPN)


async def run_topn(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        names = await topn_names(state)
        async with state.redis.pipeline(transaction=True) as p:
            p.delete(TOPN_KEY)
            if names:
                p.rpush(TOPN_KEY, *names)
            await p.execute()
        return {"names": len(names)}
    return await _run(state, "steam_topn", go)


async def run_priceoverview(state: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        now = now_utc()
        host = state.gate.scope(steam_market.URL)
        # Con cooldown activo, salir antes de LMOVE/try_spend: si no, cada tick rotaba un nombre y
        # gastaba 1 del presupuesto para que gate.acquire lo parase después.
        if left := await state.gate.cooldown_left(host):
            raise HostCoolingDown(host, left)
        quotes: list[Quote] = []
        misses, stop = 0, ""
        # Nunca más vueltas que nombres: con una lista corta, un tick no repite el mismo ítem.
        for _ in range(min(settings.STEAM_PRICEOVERVIEW_PER_TICK, await state.redis.llen(TOPN_KEY))):
            raw_name = await state.redis.lmove(TOPN_KEY, TOPN_KEY, "LEFT", "RIGHT")  # rotación atómica
            name = raw_name.decode() if isinstance(raw_name, bytes) else str(raw_name)
            if await state.redis.exists(MISS_KEY.format(name=name)):
                continue
            if not await state.gate.try_spend(BUDGET_KIND, settings.STEAM_BUDGET_PRICEOVERVIEW):
                stop = "presupuesto agotado"
                break
            try:
                q = adapter.adapt_priceoverview(await steam_market.priceoverview(state, name), name, now)
            except RateLimited:
                # Para medir la ventana de Steam: cuántas iban en este tick y en cuál cayó.
                log.warning("[steam-topn] 429 en la petición %d del tick, nombre=%r, quotes=%d misses=%d",
                            len(quotes) + misses + 1, name, len(quotes), misses)
                raise
            except (steam_market.PriceUnavailable, retry.ServerError) as e:
                log.info("[steam-topn] %s: %s", name, e)
                q = None
            if q is None:
                misses += 1
                await state.redis.set(MISS_KEY.format(name=name), 1, ex=MISS_TTL_S)
            else:
                quotes.append(q)
        stored = await market.insert_quotes(state.pg, quotes) if quotes else 0
        res = {"quotes": stored, "misses": misses, "spent_today": await state.gate.spent(BUDGET_KIND)}
        if stop:
            res["stop"] = stop
        return res
    return await _run(state, "steam_priceoverview", go)


@broker.task(task_name="steam_topn", schedule=[{"cron": "23 * * * *"}])
async def steam_topn(context: Ctx) -> dict[str, Any]:
    return await run_topn(context.state)


@broker.task(task_name="steam_priceoverview", schedule=[{"cron": settings.STEAM_PRICEOVERVIEW_CRON}])
async def steam_priceoverview(context: Ctx) -> dict[str, Any]:
    return await run_priceoverview(context.state)
