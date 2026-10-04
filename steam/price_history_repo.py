"""Capa Supabase para la captura de precios históricos por-skin.

Dos tablas: tracked_skins (qué seguimos) y precios_historicos (la serie).
supabase-py es síncrono → todas las llamadas se envuelven en asyncio.to_thread.
Cliente cacheado module-level con service_role (bypassa RLS), patrón
steam/cap_history_repo.py.
"""
import asyncio
from datetime import datetime, timezone

from supabase import create_client, Client

from settings import SUPABASE_URL, SUPABASE_SERVICE_KEY

_TRACKED = "tracked_skins"
_PRICES = "precios_historicos"
_client: Client | None = None


def get_supabase() -> Client:
    global _client
    if _client is None:
        if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_SERVICE_KEY no configuradas — "
                "no se puede acceder a la captura de precios"
            )
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


_QUEUE = "price_tick_queue"   # vista: docs/sql/tracked_skins_prioridad.sql
# El filtro `in_` viaja en la URL de PostgREST: un inventario de cientos de
# skins en una sola petición puede pasarse del límite de longitud.
_IN_CHUNK = 100


async def register_tracked(names: list[str], source: str) -> None:
    """Registra nombres en tracked_skins y refresca su `last_seen`. No-op si vacío.

    Dos pasos: el alta no pisa `source`, `first_seen` ni `last_captured` de las
    existentes; el update marca que siguen vivas (PERF-11). Sin `last_seen`, una
    skin que nadie vuelve a ver seguiría gastando cuota cada día para siempre.
    `inventory_seen_at` solo lo sube /inventory: `source` guarda el PRIMER origen,
    así que una skin que entró por el trending y luego aparece en un inventario
    no se distinguiría sin él.
    """
    if not names:
        return
    unicos = list(dict.fromkeys(names))
    rows = [{"market_hash_name": n, "source": source} for n in unicos]
    ahora = datetime.now(timezone.utc).isoformat()
    vivo = {"last_seen": ahora}
    if source == "inventory":
        vivo["inventory_seen_at"] = ahora

    def _do() -> None:
        sb = get_supabase()
        (sb.table(_TRACKED)
            .upsert(rows, on_conflict="market_hash_name", ignore_duplicates=True)
            .execute())
        for i in range(0, len(unicos), _IN_CHUNK):
            (sb.table(_TRACKED)
                .update(vivo)
                .in_("market_hash_name", unicos[i:i + _IN_CHUNK])
                .execute())

    await asyncio.to_thread(_do)


async def fetch_tracked(limit: int, before: str | None = None) -> list[str]:
    """Hasta `limit` nombres de la cola del price-tick, en orden de prioridad.

    Lee la vista `price_tick_queue`, que ya excluye las skins que nadie ha
    visto en 30 días (salvo con alerta activa) y calcula `prioridad`:
    0 alerta activa, 1 en un inventario reciente, 2 el resto. Dentro de cada
    prioridad, menos-recientemente-capturadas primero (nulls primero).

    `before` (fecha ISO) excluye las ya capturadas ese día. Es lo que hace que
    el price-tick pueda trocearse: cada llamada devuelve el siguiente lote de
    pendientes en vez de repetir siempre las mismas.
    """
    def _do() -> list[str]:
        q = (get_supabase().table(_QUEUE)
             .select("market_hash_name")
             .order("prioridad", desc=False)
             .order("last_captured", desc=False, nullsfirst=True)
             .limit(limit))
        if before is not None:
            # `or` de PostgREST: null (nunca capturada) o anterior a `before`.
            q = q.or_(f"last_captured.is.null,last_captured.lt.{before}")
        resp = q.execute()
        return [r["market_hash_name"] for r in (resp.data or [])]

    return await asyncio.to_thread(_do)


async def count_pending(before: str) -> int:
    """Skins de la cola sin capturar en la fecha `before` (ISO). Cursor del troceado."""
    def _do() -> int:
        resp = (get_supabase().table(_QUEUE)
                .select("market_hash_name", count="exact")
                .or_(f"last_captured.is.null,last_captured.lt.{before}")
                .execute())
        return resp.count or 0

    return await asyncio.to_thread(_do)


async def count_captured_on(date_iso: str) -> int:
    """Lookups que el price-tick ya gastó el día `date_iso` (PERF-11).

    Cada skin intentada queda con last_captured = hoy, con éxito o sin él, y
    cada intento es una request a steamwebapi. Contarlas en la BD hace que el
    presupuesto diario se respete a través de los lotes sin estado en memoria
    (Render reinicia el proceso cuando quiere).
    """
    def _do() -> int:
        resp = (get_supabase().table(_TRACKED)
                .select("market_hash_name", count="exact")
                .eq("last_captured", date_iso)
                .execute())
        return resp.count or 0

    return await asyncio.to_thread(_do)


async def upsert_prices(rows: list[dict]) -> None:
    """Upsert de snapshots por (market_hash_name, date). No-op si vacío."""
    if not rows:
        return

    def _do() -> None:
        (get_supabase().table(_PRICES)
            .upsert(rows, on_conflict="market_hash_name,date")
            .execute())

    await asyncio.to_thread(_do)


async def fetch_prices(name: str, limit: int = 400) -> list[dict]:
    """Serie histórica de una skin, ascendente por fecha.

    Devuelve `[{"date": str, "price": float, "volume": int|None}, ...]` — la
    misma forma que `pricing.fetch_history_for_item`, para que los consumidores
    (predicción, histórico) puedan alternar entre ambas fuentes sin traducir.
    """
    def _do() -> list[dict]:
        resp = (get_supabase().table(_PRICES)
                .select("date,price,volume")
                .eq("market_hash_name", name)
                .order("date", desc=False)
                .limit(limit)
                .execute())
        return [
            {
                "date": r["date"],
                "price": float(r["price"]),
                "volume": r.get("volume"),
            }
            for r in (resp.data or [])
        ]

    return await asyncio.to_thread(_do)


async def mark_captured(names: list[str], date_iso: str) -> None:
    """Marca last_captured=date_iso para los nombres dados. No-op si vacío."""
    if not names:
        return

    def _do() -> None:
        (get_supabase().table(_TRACKED)
            .update({"last_captured": date_iso})
            .in_("market_hash_name", names)
            .execute())

    await asyncio.to_thread(_do)


async def is_tracked(name: str) -> bool:
    def _do() -> bool:
        resp = (get_supabase().table(_TRACKED)
                .select("market_hash_name")
                .eq("market_hash_name", name)
                .limit(1)
                .execute())
        return bool(resp.data)

    return await asyncio.to_thread(_do)


async def count_tracked() -> int:
    """Número de filas en tracked_skins (para el seed idempotente)."""
    def _do() -> int:
        resp = (get_supabase().table(_TRACKED)
                .select("market_hash_name", count="exact")
                .execute())
        return resp.count or 0

    return await asyncio.to_thread(_do)
