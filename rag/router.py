"""Router del RAG: solo la ingesta programada.

`POST /rag/ask` se eliminó: el chat (`/rag/chat`) hace el retrieval en cada
mensaje e inyecta el contexto en el system prompt, además de exponer la tool
`buscar_contexto_rag` y devolver las mismas `sources[]` estructuradas. La
recuperación (`rag/retrieval.py`) sigue viva; solo desapareció el endpoint
paralelo y su generación dedicada (`rag/generation.py`).
"""
import logging
import secrets

from fastapi import APIRouter, Header, HTTPException, Request

from settings import RAG_INGEST_TOKEN
from .ingest import ingest

logger = logging.getLogger("uvicorn.error")

router = APIRouter()


@router.post("/internal/rag-ingest", summary="Ingesta de noticias del RAG (cron)")
async def rag_ingest(
    request: Request,
    x_rag_ingest_token: str = Header(default=""),
):
    try:
        valid = bool(RAG_INGEST_TOKEN) and secrets.compare_digest(
            x_rag_ingest_token.encode(), RAG_INGEST_TOKEN.encode()
        )
    except TypeError:
        valid = False
    if not valid:
        raise HTTPException(status_code=401, detail="Token inválido")
    return await ingest(request.app.state.http_client)
