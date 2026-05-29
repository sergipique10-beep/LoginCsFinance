import logging
from contextlib import asynccontextmanager
import uvicorn

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from settings import FRONTEND_URL, JWT_SECRET, STEAM_API_KEY
from middleware import SecurityHeadersMiddleware
from auth.router import router as auth_router
from steam.router import router as steam_router
from steam.services import _fetch_static_images

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
    app.state.http_client = httpx.AsyncClient(timeout=10.0)
    await _fetch_static_images(app.state.http_client)
    yield
    await app.state.http_client.aclose()


app = FastAPI(title="Steam Login", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(auth_router)
app.include_router(steam_router)


@app.get("/")
def root():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
