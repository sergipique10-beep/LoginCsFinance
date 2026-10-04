"""Noticias de CS2 (GET /news/cs2) con su og:image y caché de 30 min (CLEAN-11)."""
import asyncio
import time

import httpx

from stores import _news_cache
from steam.clients import steam_news
from steam.errors.handling import log_degraded
from steam.domain.models import NewsItem
from steam.mappers.news import _map_news_item, is_readable_news

# Cuántas noticias se piden de más para poder descartar las no latinas (UX-05),
# y tope duro para que un `count` alto no dispare una petición enorme a Steam.
NEWS_OVERFETCH = 3
NEWS_MAX_FETCH = 30


async def get_cs2_news(client: httpx.AsyncClient, count: int) -> list[NewsItem]:
    # PERF-06: sin caché cada petición costaba 5,6–12,3 s desde Render (Steam +
    # un GET por noticia para el og:image). Misma forma que _inventory_cache.
    now = time.monotonic()
    hit = _news_cache.fresh(count, now)
    if hit is not None:
        return hit

    # UX-05: se piden más de las necesarias porque después se descartan las
    # que no están en alfabeto latino. El tope evita que un `count` alto
    # dispare una petición enorme a Steam.
    fetch_count = min(count * NEWS_OVERFETCH, NEWS_MAX_FETCH)
    data = await steam_news.get_news(client, fetch_count)

    newsitems = data.get("appnews", {}).get("newsitems", [])

    # UX-05: fuera las ilegibles (ruso, chino...), y recorte al `count` pedido.
    # Si el filtro se lo lleva TODO nos quedamos con las originales: más vale una
    # noticia en ruso que un feed vacío.
    readable = [n for n in newsitems if is_readable_news(n)]
    newsitems = (readable or newsitems)[:count]
    images = await asyncio.gather(*[
        steam_news.fetch_og_image(client, item.get("url", ""))
        for item in newsitems
    ])
    for item, image in zip(newsitems, images, strict=True):
        # Una noticia sin URL no tiene página de la que sacar la imagen: no es degradación.
        if item.get("url") and not image:
            log_degraded("news_image", "og_image", "empty")
    items = [_map_news_item(item, i, images[i]) for i, item in enumerate(newsitems)]
    _news_cache.put(count, items, now)
    return items
