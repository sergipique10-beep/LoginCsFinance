"""
Persistencia de alertas de precio por skin (tabla price_alerts, Supabase).

Una alerta es de UN SOLO DISPARO: `triggered_at` null = activa; con fecha =
disparada y fuera de la rueda de evaluación. `last_checked_at` es el cursor
LRU del tick, mismo patrón que `last_captured` en tracked_skins.

Reutiliza el cliente Supabase cacheado de steam/cap_history_repo.py.
"""
import asyncio
from datetime import datetime, timezone

from steam.cap_history_repo import get_supabase

_TABLE = "price_alerts"
_COLS = "id, steam_id, market_hash_name, direction, threshold, created_at, triggered_at, triggered_price"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def list_for_user(steam_id: str) -> list[dict]:
    """Alertas del usuario: activas primero, luego disparadas; las más recientes antes."""
    def _do() -> list[dict]:
        resp = (get_supabase().table(_TABLE)
                .select(_COLS)
                .eq("steam_id", steam_id)
                .order("triggered_at", desc=False, nullsfirst=True)
                .order("created_at", desc=True)
                .execute())
        return resp.data or []

    return await asyncio.to_thread(_do)


async def count_active(steam_id: str | None = None) -> int:
    """Alertas activas; de un usuario si se pasa steam_id, del sistema si no."""
    def _do() -> int:
        q = (get_supabase().table(_TABLE)
             .select("id", count="exact")
             .is_("triggered_at", "null"))
        if steam_id is not None:
            q = q.eq("steam_id", steam_id)
        return q.execute().count or 0

    return await asyncio.to_thread(_do)


async def exists_active(steam_id: str, market_hash_name: str, direction: str, threshold: float) -> bool:
    def _do() -> bool:
        resp = (get_supabase().table(_TABLE)
                .select("id")
                .eq("steam_id", steam_id)
                .eq("market_hash_name", market_hash_name)
                .eq("direction", direction)
                .eq("threshold", threshold)
                .is_("triggered_at", "null")
                .limit(1)
                .execute())
        return bool(resp.data)

    return await asyncio.to_thread(_do)


async def create(steam_id: str, market_hash_name: str, direction: str, threshold: float) -> dict:
    def _do() -> dict:
        resp = (get_supabase().table(_TABLE)
                .insert({
                    "steam_id": steam_id,
                    "market_hash_name": market_hash_name,
                    "direction": direction,
                    "threshold": threshold,
                })
                .execute())
        return resp.data[0]

    return await asyncio.to_thread(_do)


async def delete(alert_id: int, steam_id: str) -> bool:
    """Borra solo si la alerta pertenece a steam_id. Devuelve si borró algo:
    el router responde 404 en caso contrario, sin revelar si el id existe."""
    def _do() -> bool:
        resp = (get_supabase().table(_TABLE)
                .delete()
                .eq("id", alert_id)
                .eq("steam_id", steam_id)
                .execute())
        return bool(resp.data)

    return await asyncio.to_thread(_do)


async def fetch_active(limit: int) -> list[dict]:
    """Hasta `limit` alertas activas, menos-recientemente-evaluadas primero (nulls primero)."""
    def _do() -> list[dict]:
        resp = (get_supabase().table(_TABLE)
                .select(_COLS)
                .is_("triggered_at", "null")
                .order("last_checked_at", desc=False, nullsfirst=True)
                .limit(limit)
                .execute())
        return resp.data or []

    return await asyncio.to_thread(_do)


async def mark_triggered(alert_id: int, price: float) -> None:
    def _do() -> None:
        (get_supabase().table(_TABLE)
            .update({"triggered_at": _now(), "triggered_price": price})
            .eq("id", alert_id)
            .execute())

    await asyncio.to_thread(_do)


async def mark_checked(ids: list[int]) -> None:
    if not ids:
        return

    def _do() -> None:
        (get_supabase().table(_TABLE)
            .update({"last_checked_at": _now()})
            .in_("id", ids)
            .execute())

    await asyncio.to_thread(_do)
