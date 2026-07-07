import logging
from contextlib import asynccontextmanager
import uvicorn

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from settings import (
    ALLOWED_CORS_ORIGINS, JWT_SECRET, STEAM_API_KEY,
    SUPABASE_URL, SUPABASE_SERVICE_KEY, CAP_TICK_TOKEN,
    FIREBASE_SERVICE_ACCOUNT_JSON, NEWS_TICK_TOKEN,
)
from middleware import SecurityHeadersMiddleware
from auth.router import router as auth_router
from steam.routes import router as steam_router
from steam.services import _fetch_static_images
from notifications.router import router as notifications_router

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if JWT_SECRET == "change-this-secret":
        logger.warning(
            "JWT_SECRET es el valor por defecto inseguro — "
            "define un secreto fuerte en .env"
        )
    if len(JWT_SECRET) < 32:
        logger.warning(
            "JWT_SECRET tiene menos de 32 caracteres — "
            "usa secrets.token_urlsafe(48) para generar un secreto seguro"
        )
    if not STEAM_API_KEY:
        logger.warning(
            "STEAM_API_KEY no está configurada — "
            "los endpoints de Steam Web API no funcionarán"
        )
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY and CAP_TICK_TOKEN):
        logger.warning(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY / CAP_TICK_TOKEN incompletas — "
            "el histórico del índice de precio (cap-history) no funcionará"
        )
    if not FIREBASE_SERVICE_ACCOUNT_JSON:
        logger.warning(
            "FIREBASE_SERVICE_ACCOUNT_JSON no está configurada — "
            "las push notifications no funcionarán"
        )
    if not NEWS_TICK_TOKEN:
        logger.warning(
            "NEWS_TICK_TOKEN no está configurada — "
            "el cron de noticias (news-tick) no funcionará"
        )
    app.state.http_client = httpx.AsyncClient(timeout=10.0)
    await _fetch_static_images(app.state.http_client)

    yield

    await app.state.http_client.aclose()


app = FastAPI(title="Steam Login", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(auth_router)
app.include_router(steam_router)
app.include_router(notifications_router)


@app.get("/")
def root():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
