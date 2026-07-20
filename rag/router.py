import logging
import secrets

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from auth.service import require_jwt, _get_client_ip, _rate_limit
from settings import RAG_INGEST_TOKEN
from .retrieval import retrieve
from .generation import generate_with_context
from .ingest import ingest

logger = logging.getLogger("uvicorn.error")

router = APIRouter()


class AskRequest(BaseModel):
    question: str


class Source(BaseModel):
    title: str = ""
    url: str = ""
    published_at: str | None = None


class AskResponse(BaseModel):
    reply: str
    sources: list[Source] = []


@router.post("/rag/ask", response_model=AskResponse, summary="Pregunta al RAG de noticias CS2")
async def rag_ask(
    payload: AskRequest,
    request: Request,
    _claims: dict = Depends(require_jwt),
):
    _rate_limit(_get_client_ip(request))

    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="La pregunta está vacía")

    client = request.app.state.http_client
    try:
        chunks = await retrieve(client, question)
        reply = await generate_with_context(client, question, chunks)
    except httpx.HTTPStatusError as exc:
        logger.warning("rag_ask Gemini %s: %s", exc.response.status_code, exc.response.text[:300])
        raise HTTPException(status_code=502, detail="El asistente no está disponible ahora mismo")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar con el asistente: {exc}")
    except RuntimeError as exc:
        logger.warning("rag_ask: %s", exc)
        raise HTTPException(status_code=503, detail="El asistente no está configurado")

    sources: list[Source] = []
    seen_urls: set[str] = set()
    for c in chunks:
        url = c.get("url", "")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append(Source(title=c.get("title", ""), url=url,
                               published_at=c.get("published_at")))
    return AskResponse(reply=reply, sources=sources)


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
