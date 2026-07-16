import secrets
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from auth.service import require_jwt
from settings import NEWS_TICK_TOKEN, BROADCAST_TOKEN
from . import repo, service

logger = logging.getLogger("uvicorn.error")

router = APIRouter()


class RegisterTokenBody(BaseModel):
    token: str
    platform: Literal["android", "ios"]


class DeleteTokenBody(BaseModel):
    token: str


class BroadcastBody(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=240)


@router.post("/notifications/register-token", summary="Registra un token FCM para push notifications")
async def register_token(body: RegisterTokenBody, _payload: dict = Depends(require_jwt)):
    await service.register_token(body.token, body.platform)
    return {"status": "ok"}


@router.post("/notifications/delete-token", summary="Elimina un token FCM al hacer logout")
async def delete_token(body: DeleteTokenBody, _payload: dict = Depends(require_jwt)):
    await repo.delete_device_token(body.token)
    return {"status": "ok"}


@router.post("/internal/news-tick", summary="Detecta noticias CS2 nuevas y envía push (cron interno)")
async def news_tick(request: Request, x_news_tick_token: str | None = Header(default=None)):
    if not NEWS_TICK_TOKEN or not x_news_tick_token or not secrets.compare_digest(x_news_tick_token, NEWS_TICK_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing news-tick token")

    return await service.check_and_notify_new_news(request.app.state.http_client)


@router.post("/internal/broadcast", summary="Envía una push manual a todos los dispositivos (anuncio)")
async def broadcast(body: BroadcastBody, x_broadcast_token: str | None = Header(default=None)):
    if not BROADCAST_TOKEN or not x_broadcast_token or not secrets.compare_digest(x_broadcast_token, BROADCAST_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing broadcast token")

    return await service.send_broadcast(title=body.title, body=body.body, data={})
