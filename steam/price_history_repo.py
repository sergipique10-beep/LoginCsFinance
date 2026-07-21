"""Capa Supabase para la captura de precios históricos por-skin.

Dos tablas: tracked_skins (qué seguimos) y precios_historicos (la serie).
supabase-py es síncrono → todas las llamadas se envuelven en asyncio.to_thread.
Cliente cacheado module-level con service_role (bypassa RLS), patrón
steam/cap_history_repo.py.
"""
import asyncio

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


async def register_tracked(names: list[str], source: str) -> None:
    """Registra nombres en tracked_skins. No-op si vacío. No pisa filas existentes."""
    if not names:
        return
    rows = [{"market_hash_name": n, "source": source} for n in dict.fromkeys(names)]

    def _do() -> None:
        (get_supabase().table(_TRACKED)
            .upsert(rows, on_conflict="market_hash_name", ignore_duplicates=True)
            .execute())

    await asyncio.to_thread(_do)


async def fetch_tracked(limit: int) -> list[str]:
    """Hasta `limit` nombres, menos-recientemente-capturados primero (nulls primero)."""
    def _do() -> list[str]:
        resp = (get_supabase().table(_TRACKED)
                .select("market_hash_name")
                .order("last_captured", desc=False, nullsfirst=True)
                .limit(limit)
                .execute())
        return [r["market_hash_name"] for r in (resp.data or [])]

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
    misma forma que `_fetch_history_for_item`, para que los consumidores
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


async def count_tracked() -> int:
    """Número de filas en tracked_skins (para el seed idempotente)."""
    def _do() -> int:
        resp = (get_supabase().table(_TRACKED)
                .select("market_hash_name", count="exact")
                .execute())
        return resp.count or 0

    return await asyncio.to_thread(_do)
