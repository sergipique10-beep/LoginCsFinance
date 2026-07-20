import logging
import secrets

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from auth.service import require_jwt, _get_client_ip, _rate_limit
from settings import RAG_INGEST_TOKEN
from .retrieval import retrieve
from .gemini import generate_reply, generate_with_context, generate_with_tools
from .ingest import ingest

# ── Tool registry (se registra una vez al importar el módulo) ────────────────
from tools.registry import get_declarations
from tools.market_tools import register_market_tools
from tools.inventory_tools import register_inventory_tools
from tools.predict_tools import register_predict_tools

register_market_tools()
register_inventory_tools()
register_predict_tools()

logger = logging.getLogger("uvicorn.error")

router = APIRouter()


class ChatTurn(BaseModel):
    role: str
    content: str = ""


class ChatRequest(BaseModel):
    message: str
    # Turnos previos de la conversación. Pydantic ignora campos extra
    # (id/status/ts que manda el frontend), así que basta con role/content.
    history: list[ChatTurn] = []


class ChatResponse(BaseModel):
    reply: str


@router.post("/rag/chat", response_model=ChatResponse, summary="Chat con Sharky (Gemini)")
async def rag_chat(
    payload: ChatRequest,
    request: Request,
    _claims: dict = Depends(require_jwt),
):
    _rate_limit(_get_client_ip(request))

    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="El mensaje está vacío")

    history = [t.model_dump() for t in payload.history]
    steam_id: str = _claims["sub"]
    tools = get_declarations()

    try:
        reply = await generate_with_tools(
            request.app.state.http_client,
            message,
            history,
            tools=tools if tools else None,
            tool_context={"steam_id": steam_id},
        )
    except httpx.HTTPStatusError as exc:
        logger.warning("Gemini devolvió %s: %s", exc.response.status_code, exc.response.text[:300])
        raise HTTPException(status_code=502, detail="El asistente no está disponible ahora mismo")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar con el asistente: {exc}")
    except RuntimeError as exc:
        logger.warning("rag_chat: %s", exc)
        raise HTTPException(status_code=503, detail="El asistente no está configurado")

    return ChatResponse(reply=reply)


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
