import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Request

from auth.service import _get_client_ip, _rate_limit
from stores import _news_cache
from ..clients import steam_news
from ..errors import SourceTimeout, SourceUnavailable, UpstreamError
from ..mappers.news import _map_news_item, is_readable_news

logger = logging.getLogger("uvicorn.error")

# Cuántas noticias se piden de más para poder descartar las no latinas (UX-05),
# y tope duro para que un `count` alto no dispare una petición enorme a Steam.
NEWS_OVERFETCH = 3
NEWS_MAX_FETCH = 30

router = APIRouter()


@router.get("/news/cs2", summary="Últimas noticias de CS2 vía Steam News API")
async def get_cs2_news(request: Request, count: int = 5):
    _rate_limit(_get_client_ip(request), bucket="news")  # SEC-16: fuera del cupo de auth

    # PERF-06: sin caché cada petición costaba 5,6–12,3 s desde Render (Steam +
    # un GET por noticia para el og:image). Misma forma que _inventory_cache.
    now = time.monotonic()
    hit = _news_cache.fresh(count, now)
    if hit is not None:
        return hit

    try:
        # UX-05: se piden más de las necesarias porque después se descartan las
        # que no están en alfabeto latino. El tope evita que un `count` alto
        # dispare una petición enorme a Steam.
        fetch_count = min(count * NEWS_OVERFETCH, NEWS_MAX_FETCH)
        data = await steam_news.get_news(request.app.state.http_client, fetch_count)
    except SourceTimeout:
        raise HTTPException(status_code=504, detail="Steam news request timed out") from None
    except SourceUnavailable as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}") from exc
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=f"Steam returned {exc.status}") from exc

    newsitems = data.get("appnews", {}).get("newsitems", [])

    # UX-05: fuera las ilegibles (ruso, chino...), y recorte al `count` pedido.
    # Si el filtro se lo lleva TODO nos quedamos con las originales: más vale una
    # noticia en ruso que un feed vacío.
    readable = [n for n in newsitems if is_readable_news(n)]
    newsitems = (readable or newsitems)[:count]
    images = await asyncio.gather(*[
        steam_news.fetch_og_image(request.app.state.http_client, item.get("url", ""))
        for item in newsitems
    ])
    items = [_map_news_item(item, i, images[i]) for i, item in enumerate(newsitems)]
    _news_cache.put(count, items, now)
    return items
