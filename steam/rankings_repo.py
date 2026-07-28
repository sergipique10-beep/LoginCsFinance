"""
Persistencia de rankings del mercado CS2 en Supabase (Postgres).

Una clase parametrizada por tabla, con una instancia por ranking
(`market_trending` y `market_movers`). Las dos usan estrategias distintas:

- **movers** — replace-all (`replace_snapshot`): DELETE + INSERT en cada tick.
  Son 20 ítems que se recalculan enteros cada 15 min, sin nada que acumular.

- **trending** — upsert por `name` (`upsert_rows`): ~500 ítems que se capturan
  cada hora y se van enriqueciendo en pasadas de 18 por un tick aparte. El
  DELETE no sirve aquí porque borraría el enriquecimiento acumulado en cada
  captura. A cambio hacen falta `seen_at` (purga de los que dejan de aparecer)
  y `enriched_at` (la rueda progresiva).

supabase-py es síncrono → todas las llamadas se envuelven en asyncio.to_thread
para no bloquear el event loop.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

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

    # ── Upsert incremental (solo lo usa trending) ───────────────────────────

    async def upsert_rows(self, rows: list[dict]) -> None:
        """Upsert por `name` (PK). No-op si vacío.

        Solo escribe las columnas presentes en el payload: PostgREST genera un
        `on conflict do update set <columnas del payload>`. Eso es lo que
        permite que la captura (precio, metadatos) y el enriquecimiento
        (deltas) toquen la misma fila sin pisarse.

        CUIDADO: si las filas de un mismo lote tienen claves distintas,
        PostgREST rellena con null las que falten. Los dicts de una llamada
        tienen que ser homogéneos — por eso el enrich-tick hace dos llamadas
        (los que tienen deltas y los que no).
        """
        if not rows:
            return

        def _do() -> None:
            (get_supabase().table(self._table)
                .upsert(rows, on_conflict="name")
                .execute())

        await asyncio.to_thread(_do)

    async def fetch_stalest(self, limit: int) -> list[str]:
        """Hasta `limit` nombres, menos-recientemente-enriquecidos primero.

        `enriched_at` null (nunca enriquecido) va primero. Mismo patrón que
        price_history_repo.fetch_tracked con `last_captured`.
        """
        def _do() -> list[str]:
            resp = (
                get_supabase()
                .table(self._table)
                .select("name")
                .order("enriched_at", desc=False, nullsfirst=True)
                .limit(limit)
                .execute()
            )
            return [r["name"] for r in (resp.data or [])]

        return await asyncio.to_thread(_do)

    async def fetch_ranked(self) -> list[dict]:
        """Todas las filas ordenadas por turnover descendente.

        Con upsert el `rank` deja de ser fiable como orden: un ítem que no
        aparece en una pasada conserva el rank viejo y ocuparía una posición
        alta como fantasma. `turnover` (precio × volumen 24 h) es el criterio
        con el que ya se ordenaba antes de recortar, ahora persistido.
        """
        def _do() -> list[dict]:
            resp = (
                get_supabase()
                .table(self._table)
                .select("*")
                .order("turnover", desc=True, nullsfirst=False)
                .execute()
            )
            return resp.data or []

        return await asyncio.to_thread(_do)

    async def purge_stale(self, days: int) -> int:
        """Borra las filas que llevan `days` sin aparecer en una captura.

        Sustituye al DELETE del replace-all: sin esto, un ítem que sale del
        ranking se quedaría en la tabla para siempre. La ventana da margen a
        los que entran y salen por fluctuaciones normales sin perder su
        enriquecimiento acumulado.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        def _do() -> int:
            resp = (
                get_supabase()
                .table(self._table)
                .delete()
                .lt("seen_at", cutoff)
                .execute()
            )
            return len(resp.data or [])

        return await asyncio.to_thread(_do)


trending_repo = RankingRepo("market_trending")
movers_repo = RankingRepo("market_movers")
