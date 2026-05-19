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

# ── Inventory mapper ──────────────────────────────────────────────────────────

def _delta_from_history(pts: list, days: int, latest: float) -> float:
    if not pts or not latest:
        return 0.0
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    past = [p for p in pts if p["date"] <= cutoff]
    if not past:
        return 0.0
    ref = past[-1]["price"]
    if not ref:
        return 0.0
    return round((latest - ref) / ref * 100, 2)


async def _fetch_history_for_item(client: httpx.AsyncClient, name: str) -> list:
    cache_key = f"{name}:10"
    now = time.monotonic()
    cached = _item_history_cache.get(cache_key)
    if cached and now - cached[1] < ITEM_HISTORY_CACHE_TTL:
        return cached[0]
    try:
        resp = await client.get(
            f"{STEAM_WEB_API}/history",
            params={"key": STEAM_API_KEY, "market_hash_name": name, "interval": "10", "format": "json"},
            timeout=8.0,
        )
        if resp.status_code != 200:
            return []
        raw = resp.json()
        if not isinstance(raw, list):
            return []
        pts = sorted(
            [{"date": p.get("createdat", "")[:10], "price": float(p.get("price") or 0), "volume": int(p.get("sold") or 0)}
             for p in raw if p.get("price")],
            key=lambda p: p["date"],
        )
        _item_history_cache[cache_key] = (pts, now)
        return pts
    except Exception:
        return []


async def _enrich_prices(client: httpx.AsyncClient, items: list) -> list:
    histories = await asyncio.gather(*[_fetch_history_for_item(client, it["name"]) for it in items])
    result = []
    for item, pts in zip(items, histories):
        if pts:
            latest = pts[-1]["price"]
            item = {
                **item,
                "priceLatest":   latest,
                "priceDelta24h": _delta_from_history(pts, 1, latest),
                "priceDelta7d":  _delta_from_history(pts, 7, latest),
                "priceDelta30d": _delta_from_history(pts, 30, latest),
            }
        result.append(item)
    return result

@app.get("/inventory", summary="Inventario CS2 del usuario autenticado")
async def get_inventory(
    request: Request,
    user: dict = Depends(require_jwt),
):
    steam_id: str = user["sub"]

    now = time.monotonic()
    cached = _inventory_cache.get(steam_id)
    if cached and now - cached[1] < INVENTORY_CACHE_TTL:
        return cached[0]

    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/inventory",
            params={
                "steam_id": steam_id,
                "game": STEAM_GAME,
                "key": STEAM_API_KEY,
                "language": "english",
                "limit": 5000,
            },
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Steam inventory request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="Inventory is private")
    if resp.status_code in (410, 411):
        return []
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Steam rate limit — retry later")
    if resp.status_code != 200:
        logger.error("steamwebapi /inventory → %s: %.500s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()

    if not isinstance(data, list):
        logger.error("steamwebapi /inventory unexpected format: %.500s", resp.text)
        raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

    items = [_map_item(item) for item in data]
    _inventory_cache[steam_id] = (items, now)
    return items

def _best_price_from_markets(prices: list) -> float:
    """Pick the best available price from the prices array (Steam market preferred)."""
    if not prices:
        return 0.0
    for p in prices:
        if "steam" in str(p.get("market") or "").lower():
            v = float(p.get("price") or p.get("value") or 0)
            if v:
                return v
    v = float(prices[0].get("price") or prices[0].get("value") or 0)
    return v


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=True)
