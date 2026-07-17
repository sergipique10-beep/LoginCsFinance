"""Recuperación de contexto para el RAG: embeddea una consulta y busca los
chunks más similares en Supabase (pgvector).

Unidad reutilizable: la fase 2 (orquestador con function calling) la expondrá
como la tool `buscar_contexto_rag`.
"""
import httpx

from rag import embeddings, repo


async def retrieve(client: httpx.AsyncClient, query: str, k: int = 5) -> list[dict]:
    """Top-k chunks más similares a `query`. Query vacía → []."""
    query = (query or "").strip()
    if not query:
        return []
    vector = await embeddings.embed_text(client, query)
    return await repo.match_chunks(vector, k)
