"""Persistencia del corpus RAG (rag_chunks) en Supabase (pgvector).

supabase-py es síncrono → todas las llamadas se envuelven en asyncio.to_thread.
Cliente cacheado a nivel módulo con service_role (bypassa RLS), igual que
steam/cap_history_repo.py.
"""
import asyncio

from supabase import create_client, Client

from settings import SUPABASE_URL, SUPABASE_SERVICE_KEY

_TABLE = "rag_chunks"
_client: Client | None = None


def get_supabase() -> Client:
    global _client
    if _client is None:
        if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_SERVICE_KEY no configuradas — "
                "no se puede acceder al corpus RAG"
            )
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


async def upsert_chunks(rows: list[dict]) -> None:
    """Upsert de chunks por (external_id, chunk_index). No-op si rows está vacío."""
    if not rows:
        return

    def _do() -> None:
        (get_supabase().table(_TABLE)
            .upsert(rows, on_conflict="external_id,chunk_index")
            .execute())

    await asyncio.to_thread(_do)


async def match_chunks(embedding: list[float], k: int = 5) -> list[dict]:
    """Top-k chunks por similitud coseno vía la función RPC match_rag_chunks."""
    def _do() -> list[dict]:
        resp = get_supabase().rpc(
            "match_rag_chunks",
            {"query_embedding": embedding, "match_count": k},
        ).execute()
        return resp.data or []

    return await asyncio.to_thread(_do)


async def seen_external_ids(external_ids: list[str]) -> set[str]:
    """Subconjunto de external_ids que ya existen en la tabla (para dedup)."""
    if not external_ids:
        return set()

    def _do() -> set[str]:
        resp = (get_supabase().table(_TABLE)
                .select("external_id")
                .in_("external_id", external_ids)
                .execute())
        return {r["external_id"] for r in (resp.data or [])}

    return await asyncio.to_thread(_do)
