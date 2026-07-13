"""
Persistencia del ranking hot/cold (movers) del mercado CS2 en Supabase (Postgres).

Mismo patrón que trending_repo.py: la tabla `public.market_movers`
se reemplaza por completo en cada tick (DELETE + INSERT), reflejando
siempre el ranking exacto del último cron. No hay historial — solo el
snapshot más reciente.

supabase-py es síncrono → todas las llamadas se envuelven en asyncio.to_thread
para no bloquear el event loop.
"""
import asyncio
import logging

from .cap_history_repo import get_supabase

logger = logging.getLogger("uvicorn.error")

_TABLE = "market_movers"


async def replace_snapshot(rows: list[dict]) -> None:
    """Reemplaza el contenido completo de la tabla con el ranking actual."""
    def _do() -> None:
        client = get_supabase()
        client.table(_TABLE).delete().neq("name", "").execute()
        if rows:
            client.table(_TABLE).insert(rows).execute()

    await asyncio.to_thread(_do)


async def fetch_snapshot() -> list[dict]:
    """Todas las filas, ordenadas por rank ascendente."""
    def _do() -> list[dict]:
        resp = (
            get_supabase()
            .table(_TABLE)
            .select("*")
            .order("rank", desc=False)
            .execute()
        )
        return resp.data or []

    return await asyncio.to_thread(_do)
