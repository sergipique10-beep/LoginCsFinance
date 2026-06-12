import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import uvicorn

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from settings import ALLOWED_CORS_ORIGINS, JWT_SECRET, STEAM_API_KEY
from middleware import SecurityHeadersMiddleware
from auth.router import router as auth_router
from steam.routes import router as steam_router
from steam.services import STEAM_WEB_API, _fetch_static_images
from stores import _market_cap_history, _CAP_HISTORY_MAX, load_cap_history, save_cap_history

logger = logging.getLogger("uvicorn.error")

_CAP_HISTORY_PATH = Path("data/market_cap_history.json")


async def _tick_market_cap(http_client: httpx.AsyncClient) -> None:
    """Hourly cron: snapshot the CS2 market priceindex."""
    try:
        resp = await http_client.get(
            f"{STEAM_WEB_API}/market-index/cs2",
            params={"key": STEAM_API_KEY, "format": "json"},
            timeout=15.0,
        )
        if resp.status_code != 200:
            logger.warning("[cap-history] market-index returned %s", resp.status_code)
            return
        data = resp.json()
        if not isinstance(data, dict):
            return
        price_index = data.get("priceindex")
        if price_index is None:
            logger.warning("[cap-history] 'priceindex' missing from response")
            return
        point = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "v": float(price_index),
        }
        _market_cap_history.append(point)
        if len(_market_cap_history) > _CAP_HISTORY_MAX:
            del _market_cap_history[:-_CAP_HISTORY_MAX]
        save_cap_history(_CAP_HISTORY_PATH)
        logger.info("[cap-history] snapshot saved: %s = %.4f", point["ts"], point["v"])
    except Exception as exc:
        logger.warning("[cap-history] tick failed: %s", exc)


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

    load_cap_history(_CAP_HISTORY_PATH)

    async def _hourly_loop():
        while True:
            now = datetime.now(timezone.utc)
            seconds_until_next_hour = (60 - now.minute) * 60 - now.second
            await asyncio.sleep(seconds_until_next_hour)
            await _tick_market_cap(app.state.http_client)

    task = asyncio.create_task(_hourly_loop())

    yield

    task.cancel()
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


@app.get("/")
def root():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
