"""Cola y planificador del worker: Taskiq sobre Valkey (COST-02).

- `RedisStreamBroker` (taskiq-redis): streams con consumer group y ack; una tarea que se cae
  a medias se reentrega a otro consumidor tras `idle_timeout`.
- `TaskiqScheduler` + `LabelScheduleSource`: las cadencias van en el decorador de cada job
  (`schedule=[{"cron": ...}]`). Un solo proceso `scheduler` por despliegue.

Los recursos compartidos (HTTP, Valkey, gate, breakers, Postgres, Supabase) se crean una vez
por proceso worker en WORKER_STARTUP y viven en `state`.
"""
import logging
from typing import Any

import httpx
from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import RedisStreamBroker

from worker import logs, settings
from worker.net.breaker import Breakers
from worker.net.gate import Gate
from worker.store import pg, redis, supabase

log = logging.getLogger(__name__)

broker = RedisStreamBroker(url=settings.VALKEY_URL, queue_name="csfinance", consumer_group_name="worker")
scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])


async def open_resources(state: Any) -> None:
    # El UA lleva el de httpx: steamcommunity.com/inventory responde 429 a los UA propios
    # ("cs-finance-worker/1.0", "Mozilla/5.0" sin cookies) y 200 a "python-httpx/x" o "curl/x"
    # desde la misma IP (medido en el VPS el 2026-10-16, COST-07/COST-12).
    ua = f"cs-finance-worker/1.0 python-httpx/{httpx.__version__}"
    # Sin `br`: con `brotli` instalado httpx anuncia "gzip, deflate, br" y steamcommunity.com responde 429
    # a esa cabecera (aislado el 2026-10-16: misma IP, mismo UA, mismo instante; sin br → 200). Skinport,
    # que exige br, lo pide por petición en worker/sources/skinport.py.
    # Sin keep-alive: el worker recibía 429 de priceoverview a las ~20 peticiones del mismo cliente mientras
    # 70 sondas con las mismas cabeceras y ritmo, cada una en conexión nueva, daban 200 (2026-10-16 23:25-23:46).
    # Hipótesis en prueba: Steam cuenta por conexión. Coste: un handshake TLS por petición, irrelevante a 6/min.
    state.http = httpx.AsyncClient(timeout=30, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate",
                                                        "Connection": "close"})
    state.redis = redis.async_client(settings.VALKEY_URL)
    state.gate = Gate(state.redis)
    state.breakers = Breakers(redis.sync_client(settings.VALKEY_URL))
    state.pg = await pg.create_pool(settings.PG_DSN) if settings.PG_DSN else None
    state.supabase = (supabase.client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
                      if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY else None)
    if state.pg is None or state.supabase is None:
        log.warning("[worker] arranque sin %s", "PG_DSN" if state.pg is None else "Supabase")


async def close_resources(state: Any) -> None:
    await state.http.aclose()
    await state.redis.aclose()
    if state.pg is not None:
        await state.pg.close()


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _startup(state: TaskiqState) -> None:
    logs.configure()
    await open_resources(state)
    log.info("[worker] recursos listos")


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _shutdown(state: TaskiqState) -> None:
    await close_resources(state)
