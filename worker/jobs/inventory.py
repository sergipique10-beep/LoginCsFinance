"""Cola prioritaria de inventarios y perfiles (COST-07). Proceso propio: `python -m worker.main inventory`.

    Render ──insert──▶ Supabase fetch_requests (status pending)
    ingress  (cada 3 s)  pending → ZSET inv:q (score = prio·1e13 + ms) → status queued
    fetcher  (UNA corutina) ZPOPMIN → inv:inflight:{kind}:{id} → descarga → status done|error

prio 0 = interactivo (GET /inventory, /me), 1 = alerta, 2 = fondo. El miembro del ZSET es
`{kind}:{steam_id}`: dos peticiones del mismo usuario son una descarga (ZADD LT se queda con la
prioridad más alta). Una sola corutina descarga: el ritmo lo pone el gate de steamcommunity.com.

Tipos:
  inventory  steamcommunity → extract + certificados (caché inv:cert:{assetid} 7 d + asset_decoded)
             → Supabase INVENTORY_SNAPSHOT_TABLE (sombra) + VPS inventory_assets
  profile    GetPlayerSummaries → Supabase steam_profiles
  delete     borra inventory_assets del usuario (DELETE /me; Render no llega a la Timescale)
"""
import asyncio
import datetime as dt
import json
import logging
import os
import time
from typing import Any

from worker import health
from worker.decode import inventory as inv_decode
from worker.net.breaker import RateLimited
from worker.net.gate import HostCoolingDown
from worker.sources import steam_inventory, steam_profile
from worker.store import inventory as inv_store
from worker.store import supabase as sb_store

log = logging.getLogger(__name__)

QUEUE = "inv:q"
CERT_TTL_S = 7 * 24 * 3600
INFLIGHT_TTL_S = 300
SNAPSHOT_TABLE = os.environ.get("INVENTORY_SNAPSHOT_TABLE", "inventory_snapshots_shadow")
KINDS = ("inventory", "profile", "delete")


def score(prio: int, created_at: str | None) -> float:
    try:
        ms = dt.datetime.fromisoformat(created_at or "").timestamp() * 1000
    except ValueError:
        ms = time.time() * 1000
    return max(0, min(int(prio), 9)) * 1e13 + ms


async def ingress_once(state: Any) -> int:
    rows = await sb_store.pending_requests(state.supabase)
    rows = [r for r in rows if r.get("kind") in KINDS and r.get("steam_id")]
    for r in rows:
        await state.redis.zadd(QUEUE, {f"{r['kind']}:{r['steam_id']}": score(r.get("prio", 2), r.get("created_at"))},
                               lt=True)
    await sb_store.set_status(state.supabase, [r["id"] for r in rows], "queued")
    return len(rows)


async def _cached_certs(redis: Any, aids: list[str]) -> dict[str, dict[str, Any]]:
    if not aids:
        return {}
    vals = await redis.mget([f"inv:cert:{a}" for a in aids])
    return {a: json.loads(v) for a, v in zip(aids, vals, strict=True) if v}


async def process_inventory(state: Any, steam_id: str) -> dict[str, Any]:
    raw = await steam_inventory.fetch(state, steam_id)
    certs = inv_decode.certificates(raw)
    cached = await _cached_certs(state.redis, list(certs))
    decoded, failed = inv_decode.decode_certs(certs, cached)
    new = [a for a in decoded if a not in cached]
    for a in new:
        await state.redis.set(f"inv:cert:{a}", json.dumps(decoded[a]), ex=CERT_TTL_S)
    items = inv_decode.extract(raw, decoded)
    if state.pg is not None:
        await inv_store.upsert_decoded(state.pg, [(a, decoded[a], certs[a]) for a in new])
        await inv_store.replace_assets(state.pg, steam_id, items)
    captured = dt.datetime.now(dt.UTC).isoformat()
    await sb_store.upsert(state.supabase, SNAPSHOT_TABLE,
                          [{"steam_id": steam_id, "items": items, "captured_at": captured}], "steam_id")
    ratio = inv_decode.decode_missing_ratio(items)
    log.info("[inventory] steam_id=%s items=%s certs_new=%s certs_failed=%s decode_missing_ratio=%s",
             steam_id, len(items), len(new), failed, ratio)
    return {"items": len(items), "decode_missing_ratio": ratio, "certs_failed": failed}


async def process_profile(state: Any, steam_id: str) -> dict[str, Any]:
    players = await steam_profile.summaries(state, [steam_id])
    rows = [{**steam_profile.to_row(p), "updated_at": dt.datetime.now(dt.UTC).isoformat()} for p in players]
    if not rows:
        raise steam_inventory.InventoryUnavailable(404)
    await sb_store.upsert(state.supabase, "steam_profiles", rows, "steam_id")
    return {"profiles": len(rows)}


async def process_delete(state: Any, steam_id: str) -> dict[str, Any]:
    if state.pg is not None:
        await inv_store.delete_user(state.pg, steam_id)
    return {"deleted": True}


HANDLERS = {"inventory": process_inventory, "profile": process_profile, "delete": process_delete}


def error_code(exc: Exception) -> str:
    """Lo que lee Render en fetch_requests.error para decidir el status HTTP."""
    if isinstance(exc, steam_inventory.InventoryPrivate):
        return "private"
    if isinstance(exc, HostCoolingDown | RateLimited):
        return "rate_limit"
    if isinstance(exc, steam_inventory.InventoryUnavailable):
        return f"http_{exc.status}"
    return "upstream"


async def fetch_once(state: Any) -> dict[str, Any] | None:
    popped = await state.redis.zpopmin(QUEUE)
    if not popped:
        return None
    member = popped[0][0].decode() if isinstance(popped[0][0], bytes) else popped[0][0]
    kind, _, steam_id = member.partition(":")
    lock = f"inv:inflight:{member}"
    if not await state.redis.set(lock, "1", nx=True, ex=INFLIGHT_TTL_S):
        return {"kind": kind, "steam_id": steam_id, "skipped": "inflight"}
    try:
        res = await HANDLERS[kind](state, steam_id)
        await sb_store.finish(state.supabase, kind, steam_id, "done")
        return {"kind": kind, "steam_id": steam_id, "ok": True, **res}
    except Exception as e:  # noqa: BLE001 — cualquier fallo cierra la petición con su código; Render decide
        code = error_code(e)
        log.warning("[inventory] kind=%s steam_id=%s error=%s %r", kind, steam_id, code, e)
        await sb_store.finish(state.supabase, kind, steam_id, "error", code)
        return {"kind": kind, "steam_id": steam_id, "ok": False, "error": code}
    finally:
        await state.redis.delete(lock)


async def run(state: Any, stop: asyncio.Event, poll_s: float = 3.0, idle_s: float = 1.0) -> None:
    """Bucle del proceso `inventory`: ingress cada `poll_s` y fetcher continuo, latido a Kuma por minuto."""
    async def ingress() -> None:
        last_beat = 0.0
        while not stop.is_set():
            try:
                n = await ingress_once(state)
                ok = True
            except Exception as e:  # noqa: BLE001 — Supabase caído: se reintenta en el siguiente poll
                log.warning("[inventory] ingress error=%r", e)
                n, ok = 0, False
            if time.monotonic() - last_beat > 60:
                await health.push(state.http, "inventory", ok, msg=f"ingress={n} queue={await state.redis.zcard(QUEUE)}")
                last_beat = time.monotonic()
            await _wait(stop, poll_s)

    async def fetcher() -> None:
        while not stop.is_set():
            if await fetch_once(state) is None:
                await _wait(stop, idle_s)

    await asyncio.gather(ingress(), fetcher())


async def _wait(stop: asyncio.Event, s: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=s)
    except TimeoutError:
        pass


async def main() -> None:
    import signal
    from types import SimpleNamespace

    from worker import broker, logs
    logs.configure()
    state = SimpleNamespace()
    await broker.open_resources(state)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await run(state, stop)
    finally:
        await broker.close_resources(state)
