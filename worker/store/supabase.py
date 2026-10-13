"""Supabase desde el worker: service_role (salta RLS), cliente síncrono en un hilo.

No reutiliza `steam/cap_history_repo.py` a propósito: la imagen del worker solo lleva
`worker/`, sin el backend de Render.
"""
import asyncio
from typing import Any

from supabase import create_client


def client(url: str, key: str) -> Any:
    return create_client(url, key)


async def select_one(sb: Any) -> bool:
    """El «SELECT 1» de PostgREST: leer una fila de una tabla que siempre existe."""
    def _q() -> Any:
        return sb.table("market_cap_history").select("ts").limit(1).execute()
    res = await asyncio.to_thread(_q)
    return isinstance(res.data, list)
