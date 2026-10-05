"""
Persistencia del histórico del índice de precio CS2 en Supabase (Postgres).

Reemplaza el antiguo store en memoria + JSON (volátil en discos efímeros como
Render free). La tabla `public.market_cap_history` tiene `ts` como PK, así que
los snapshots son idempotentes por hora (upsert).

supabase-py es síncrono → todas las llamadas se envuelven en asyncio.to_thread
para no bloquear el event loop.
"""
import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

import httpx
from postgrest.exceptions import APIError
from supabase import create_client, Client

from settings import SUPABASE_URL, SUPABASE_SERVICE_KEY
from steam.errors import StorageError

logger = logging.getLogger("uvicorn.error")

_TABLE = "market_cap_history"

T = TypeVar("T")

_client: Client | None = None


def get_supabase() -> Client:
    """Cliente Supabase cacheado a nivel módulo (service_role → bypassa RLS)."""
    global _client
    if _client is None:
        if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_SERVICE_KEY no configuradas — "
                "no se puede acceder al histórico de cap-history"
            )
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


# Lo que puede fallar al hablar con Supabase: postgrest (fila rechazada, tabla que no
# existe), red (httpx por debajo de supabase-py) y `get_supabase` sin credenciales.
_STORAGE_FAILURES = (APIError, httpx.HTTPError, RuntimeError)


async def storage_call(fn: Callable[[], T]) -> T:
    """Ejecuta una operación síncrona de supabase-py en un hilo y traduce sus fallos a
    `StorageError`, para que un llamador best-effort capture eso y no
    `Exception`. Lo usan solo los caminos que no deben romper la respuesta (snapshot del
    inventario, `register_tracked`); los ticks dejan subir el error tal cual."""
    try:
        return await asyncio.to_thread(fn)
    except _STORAGE_FAILURES as exc:
        raise StorageError(f"{type(exc).__name__}: {exc}") from exc


async def insert_snapshot(point: dict) -> None:
    """Upsert de un snapshot por `ts` (idempotente dentro de la misma hora)."""
    def _do() -> None:
        get_supabase().table(_TABLE).upsert(point, on_conflict="ts").execute()

    await asyncio.to_thread(_do)


async def fetch_range(cutoff: datetime) -> list[dict]:
    """Filas con ts >= cutoff, ordenadas ascendentemente por ts."""
    def _do() -> list[dict]:
        resp = (
            get_supabase()
            .table(_TABLE)
            .select("*")
            .gte("ts", cutoff.isoformat())
            .order("ts", desc=False)
            .execute()
        )
        return resp.data or []

    return await asyncio.to_thread(_do)
