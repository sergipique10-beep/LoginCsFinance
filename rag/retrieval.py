"""Recuperación de contexto para el RAG: embeddea una consulta y busca los
chunks más similares en Supabase (pgvector).

Unidad reutilizable: la fase 2 (orquestador con function calling) la expondrá
como la tool `buscar_contexto_rag`.
"""
import httpx

from rag import embeddings, repo
from settings import RAG_MIN_SIMILARITY


async def retrieve(client: httpx.AsyncClient, query: str, k: int = 5) -> list[dict]:
    """Top-k chunks más similares a `query`. Query vacía → [].

    Filtra por RAG_MIN_SIMILARITY: un chunk con similitud por debajo del
    umbral no es relevante y no debe colarse en `sources`.
    """
    query = (query or "").strip()
    if not query:
        return []
    vector = await embeddings.embed_text(client, query)
    rows = await repo.match_chunks(vector, k)
    return [r for r in rows if r.get("similarity", 0) >= RAG_MIN_SIMILARITY]
