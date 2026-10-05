"""
Último inventario bueno por usuario (tabla inventory_snapshots, Supabase) — PERF-14.

Sirve para degradar con elegancia ante un 429 de steamwebapi: se devuelve esto, con
su `captured_at` real, en vez de un error. Una fila por usuario, sobrescrita en cada
lectura con 200. Reutiliza el cliente Supabase cacheado de steam/cap_history_repo.py.
"""
import asyncio
from datetime import datetime, timezone

from steam.cap_history_repo import get_supabase, storage_call

_TABLE = "inventory_snapshots"


async def save(steam_id: str, items: list) -> None:
    def _do() -> None:
        fila = {
            "steam_id": steam_id,
            "items": items,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        get_supabase().table(_TABLE).upsert(fila, on_conflict="steam_id").execute()

    await storage_call(_do)   # falla con StorageError: la ruta lo captura, best-effort


async def load(steam_id: str) -> tuple[list, str] | None:
    """(items, captured_at ISO-8601) o None si el usuario nunca tuvo una lectura buena."""
    def _do() -> tuple[list, str] | None:
        resp = (get_supabase().table(_TABLE)
                .select("items, captured_at")
                .eq("steam_id", steam_id)
                .limit(1)
                .execute())
        fila = (resp.data or [None])[0]
        return (fila["items"], fila["captured_at"]) if fila else None

    return await storage_call(_do)


async def delete_for_user(steam_id: str) -> None:
    """Borrado de cuenta (LAUNCH-04)."""
    def _do() -> None:
        get_supabase().table(_TABLE).delete().eq("steam_id", steam_id).execute()

    await asyncio.to_thread(_do)
