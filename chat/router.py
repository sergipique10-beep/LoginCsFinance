import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth.service import require_jwt, _get_client_ip, _rate_limit
from chat.agent import generate_with_tools

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
            request.app.state.http_client, message, history,
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
