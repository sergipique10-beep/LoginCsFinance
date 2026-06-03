import asyncio
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

from auth.service import _get_client_ip, _rate_limit
from ..mappers import _map_news_item, _fetch_og_image

logger = logging.getLogger("uvicorn.error")

router = APIRouter()


@router.get("/news/cs2", summary="Últimas noticias de CS2 vía Steam News API")
async def get_cs2_news(request: Request, count: int = 5):
    _rate_limit(_get_client_ip(request))

    try:
        resp = await request.app.state.http_client.get(
            "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
            params={"appid": 730, "count": count, "format": "json"},
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Steam news request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    newsitems = resp.json().get("appnews", {}).get("newsitems", [])
    images = await asyncio.gather(*[
        _fetch_og_image(request.app.state.http_client, item.get("url", ""))
        for item in newsitems
    ])
    return [_map_news_item(item, i, images[i]) for i, item in enumerate(newsitems)]
