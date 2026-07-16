"""
Persistencia de rankings del mercado CS2 en Supabase (Postgres).

Un mismo patrón sirve para las dos tablas de ranking (`market_trending` y
`market_movers`): en cada tick se reemplaza el contenido completo de la tabla
(DELETE + INSERT), reflejando siempre el snapshot exacto del último cron. No hay
historial — solo el snapshot más reciente. Antes eran dos módulos idénticos
(trending_repo.py / movers_repo.py) que solo diferían en el nombre de la tabla;
ahora es una clase parametrizada por tabla, con una instancia por ranking.

supabase-py es síncrono → todas las llamadas se envuelven en asyncio.to_thread
para no bloquear el event loop.
"""
import asyncio
import logging

from .cap_history_repo import get_supabase

logger = logging.getLogger("uvicorn.error")


class RankingRepo:
    """Repo de snapshot (replace-all) para una tabla de ranking concreta."""

    def __init__(self, table: str):
        self._table = table

    async def replace_snapshot(self, rows: list[dict]) -> None:
        """Reemplaza el contenido completo de la tabla con el ranking actual."""
        def _do() -> None:
            client = get_supabase()
            client.table(self._table).delete().neq("name", "").execute()
            if rows:
                client.table(self._table).insert(rows).execute()

        await asyncio.to_thread(_do)

    async def fetch_snapshot(self) -> list[dict]:
        """Todas las filas, ordenadas por rank ascendente."""
        def _do() -> list[dict]:
            resp = (
                get_supabase()
                .table(self._table)
                .select("*")
                .order("rank", desc=False)
                .execute()
            )
            return resp.data or []

        return await asyncio.to_thread(_do)


trending_repo = RankingRepo("market_trending")
movers_repo = RankingRepo("market_movers")
