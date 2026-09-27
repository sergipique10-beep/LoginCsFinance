import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth.service import require_jwt, _get_client_ip, _rate_limit
from chat.agent import generate_with_sources
from settings import CHAT_ENABLED

from tools.registry import get_declarations
from tools.market_tools import register_market_tools
from tools.inventory_tools import register_inventory_tools
from tools.predict_tools import register_predict_tools
from tools.rag_tools import register_rag_tools

register_market_tools()
register_inventory_tools()
register_predict_tools()
register_rag_tools()

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


class Source(BaseModel):
    title: str = ""
    url: str = ""
    published_at: str | None = None


class ChatStatus(BaseModel):
    """Estado de Sharky, para que el frontend sepa si pintar la UI del chat.

    Público a propósito (sin `require_jwt`): la app necesita saber si mostrar el
    botón del chat antes de que haya sesión, y la respuesta no revela nada.
    """
    enabled: bool


class ChatResponse(BaseModel):
    reply: str
    # Fuentes del contexto RAG recuperado para este mensaje. Sustituye al
    # `sources[]` que antes solo daba /rag/ask: el dato viaja estructurado, sin
    # depender de que el modelo lo cite en prosa. Vacío si no hubo contexto.
    sources: list[Source] = []


async def require_chat_enabled():
    """404 si Sharky está apagado (PERF-04).

    Va como dependencia y **antes** de `require_jwt` en la lista: si fuera un
    guard dentro del cuerpo, `require_jwt` se evaluaría primero y un anónimo
    recibiría 401, revelando que el endpoint existe y espera credenciales.
    Medido: con el guard en el cuerpo, `POST /rag/chat` sin token daba 401.
    """
    if not CHAT_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/rag/chat/status", response_model=ChatStatus, summary="¿Está Sharky disponible?")
async def rag_chat_status():
    return ChatStatus(enabled=CHAT_ENABLED)


@router.post(
    "/rag/chat",
    response_model=ChatResponse,
    summary="Chat con Sharky (Gemini)",
    # El orden importa: el flag se evalúa antes que la auth, así un anónimo ve
    # 404 y no 401 cuando el chat está apagado.
    dependencies=[Depends(require_chat_enabled)],
)
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
        reply, fragmentos = await generate_with_sources(
            request.app.state.http_client, message, history,
            tools=tools if tools else None,
            tool_context={"steam_id": steam_id},
        )
    except httpx.HTTPStatusError as exc:
        logger.warning("Gemini devolvió %s: %s", exc.response.status_code, exc.response.text[:300])
        # La cuota agotada no es una caída: el free tier son 20 req/día por
        # proyecto y el loop de tools gasta 2-4 por mensaje (PERF-04). Devolverlo
        # como 502 genérico le enseña al usuario que la app falla, cuando lo que
        # pasa es que se acabó el cupo del día. 429 para que el frontend pueda
        # distinguirlo y decirlo con sus palabras.
        if exc.response.status_code == 429:
            raise HTTPException(
                status_code=429,
                detail="El asistente ha alcanzado su límite de consultas diarias",
            )
        raise HTTPException(status_code=502, detail="El asistente no está disponible ahora mismo")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar con el asistente: {exc}")
    except RuntimeError as exc:
        logger.warning("rag_chat: %s", exc)
        raise HTTPException(status_code=503, detail="El asistente no está configurado")

    sources: list[Source] = []
    seen_urls: set[str] = set()
    for c in fragmentos:
        url = c.get("url", "")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append(Source(title=c.get("title", ""), url=url,
                              published_at=c.get("published_at")))
    return ChatResponse(reply=reply, sources=sources)
