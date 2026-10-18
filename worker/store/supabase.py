"""Supabase desde el worker: service_role (salta RLS), cliente síncrono en un hilo.

No reutiliza `steam/cap_history_repo.py` a propósito: la imagen del worker solo lleva
`worker/`, sin el backend de Render.
"""
import asyncio
import datetime as dt
from typing import Any

from supabase import create_client


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def client(url: str, key: str) -> Any:
    return create_client(url, key)


async def select_one(sb: Any) -> bool:
    """El «SELECT 1» de PostgREST: leer una fila de una tabla que siempre existe."""
    def _q() -> Any:
        return sb.table("market_cap_history").select("ts").limit(1).execute()
    res = await asyncio.to_thread(_q)
    return isinstance(res.data, list)


async def upsert(sb: Any, table: str, rows: list[dict[str, Any]], on_conflict: str, batch: int = 500) -> int:
    """Upsert por lotes (PostgREST corta peticiones grandes). Devuelve filas enviadas."""
    def _q(chunk: list[dict[str, Any]]) -> Any:
        return sb.table(table).upsert(chunk, on_conflict=on_conflict).execute()
    for i in range(0, len(rows), batch):
        await asyncio.to_thread(_q, rows[i:i + batch])
    return len(rows)


async def pending_requests(sb: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Peticiones de Render (fetch_requests) sin encolar, por prioridad y antigüedad."""
    def _q() -> Any:
        return (sb.table("fetch_requests").select("id, kind, steam_id, prio, created_at")
                .eq("status", "pending").order("prio").order("created_at").limit(limit).execute())
    return list((await asyncio.to_thread(_q)).data or [])


async def set_status(sb: Any, ids: list[int], status: str) -> None:
    if not ids:
        return
    def _q() -> Any:
        return sb.table("fetch_requests").update({"status": status, "updated_at": _now()}).in_("id", ids).execute()
    await asyncio.to_thread(_q)


async def finish(sb: Any, kind: str, steam_id: str, status: str, error: str | None = None) -> None:
    """Cierra TODAS las peticiones abiertas de (kind, steam_id): una descarga atiende a todas."""
    def _q() -> Any:
        return (sb.table("fetch_requests").update({"status": status, "error": error, "updated_at": _now()})
                .eq("kind", kind).eq("steam_id", steam_id).in_("status", ["pending", "queued"]).execute())
    await asyncio.to_thread(_q)


async def active_alert_names(sb: Any) -> list[str]:
    """Nombres con alguna alerta de precio sin disparar (prio 0 del top-N de COST-04)."""
    def _q() -> Any:
        return sb.table("price_alerts").select("market_hash_name").is_("triggered_at", "null").execute()
    rows = (await asyncio.to_thread(_q)).data or []
    return sorted({r["market_hash_name"] for r in rows if r.get("market_hash_name")})



# ---------------------------------------------------------------- rankings, índice y cierre (COST-05)

async def select_all(sb: Any, table: str, columns: str, page: int = 1000, **eq: Any) -> list[dict[str, Any]]:
    """Toda la tabla por páginas (PostgREST corta en `db-max-rows`, 1000 en Supabase)."""
    def _q(lo: int) -> Any:
        q = sb.table(table).select(columns)
        for k, v in eq.items():
            q = q.eq(k, v)
        return q.range(lo, lo + page - 1).execute()
    out: list[dict[str, Any]] = []
    lo = 0
    while True:
        rows = list((await asyncio.to_thread(_q, lo)).data or [])
        out += rows
        if len(rows) < page:
            return out
        lo += page


async def last_cap_point(sb: Any, table: str = "market_cap_history") -> dict[str, Any] | None:
    """El último punto del índice de steamwebapi: el I_0 del índice propio."""
    def _q() -> Any:
        return sb.table(table).select("ts, priceindex, realpriceindex").order("ts", desc=True).limit(1).execute()
    rows = (await asyncio.to_thread(_q)).data or []
    return rows[0] if rows else None


async def cap_points_since(sb: Any, table: str, since_iso: str) -> list[dict[str, Any]]:
    """Puntos horarios del índice desde `since_iso` (shadow_compare: signo diario de las dos series)."""
    def _q() -> Any:
        return sb.table(table).select("ts, priceindex").gte("ts", since_iso).order("ts").execute()
    return list((await asyncio.to_thread(_q)).data or [])


async def replace_all(sb: Any, table: str, rows: list[dict[str, Any]], key: str = "name") -> int:
    """DELETE de todo + INSERT (los movers de hoy: `RankingRepo.replace_snapshot`)."""
    def _q() -> Any:
        sb.table(table).delete().neq(key, "").execute()
        if rows:
            sb.table(table).insert(rows).execute()
        return len(rows)
    return int(await asyncio.to_thread(_q))


async def delete_older(sb: Any, table: str, column: str, before_iso: str) -> None:
    """La purga del trending: fuera lo que no aparece desde `before_iso`."""
    def _q() -> Any:
        return sb.table(table).delete().lt(column, before_iso).execute()
    await asyncio.to_thread(_q)
