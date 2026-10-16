"""Jobs del worker. COST-02 solo trae `noop`: el latido que demuestra que la tubería funciona.

Cada job termina con un push a Kuma (`health.push`), con éxito o sin él: un monitor push que
deja de recibir latidos es la alarma de «el worker no corre».
"""
import logging
import time
from typing import Annotated, Any

from taskiq import Context, TaskiqDepends

from worker import health
from worker.broker import broker
from worker.store import pg, supabase

log = logging.getLogger(__name__)


async def run_noop(state: Any) -> dict[str, Any]:
    t0 = time.monotonic()
    checks: dict[str, Any] = {}
    for name, fn, res in (("supabase", supabase.select_one, state.supabase), ("pg", pg.select_one, state.pg)):
        try:
            checks[name] = await fn(res) if res is not None else None
        except Exception as e:  # noqa: BLE001 — el latido informa de cualquier fallo, no lo propaga
            checks[name] = repr(e)[:120]
    ok = all(v is True for v in checks.values())
    ms = 1000 * (time.monotonic() - t0)
    await health.push(state.http, "noop", ok, msg=" ".join(f"{k}={v}" for k, v in checks.items()), ping_ms=ms)
    log.info("[noop] ok=%s %s ms=%.0f", ok, checks, ms)
    return {"ok": ok, **checks}


@broker.task(task_name="noop", schedule=[{"cron": "* * * * *"}])
async def noop(context: Annotated[Context, TaskiqDepends()]) -> dict[str, Any]:
    return await run_noop(context.state)


# Registra los jobs de los módulos hermanos en el broker (la CLI de Taskiq importa este paquete).
from worker.jobs import market_sync, steam_topn  # noqa: E402, F401
