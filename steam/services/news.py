"""Noticias de CS2 (GET /news/cs2) con su og:image y caché de 30 min (CLEAN-11)."""
import asyncio
import time

import httpx

from steam.cache.user_cache import _news_cache
from steam.adapters.news_adapter import adapt_news
from steam.api import news_client
from steam.errors.handling import DEGRADABLE, log_degraded, reason_of
from steam.domain.models import Fetched, NewsItem
from steam.mappers.news_mapper import _map_news_item, is_readable_news

# Cuántas noticias se piden de más para poder descartar las no latinas (UX-05),
# y tope duro para que un `count` alto no dispare una petición enorme a Steam.
NEWS_OVERFETCH = 3
NEWS_MAX_FETCH = 30


async def _og_image(client: httpx.AsyncClient, url: str) -> str:
    """"" si la noticia no tiene URL (no es degradación), si la página no trae og:image
    (`no_og_tag`) o si falló (motivo de `reason_of`): la noticia se pinta igual."""
    if not url:
        return ""
    try:
        image = await news_client.fetch_og_image(client, url)
    except DEGRADABLE as exc:
        log_degraded("news_image", reason_of(exc), "empty")
        return ""
    if not image:
        log_degraded("news_image", "no_og_tag", "empty")
    return image


async def get_cs2_news(client: httpx.AsyncClient, count: int) -> Fetched[list[NewsItem]]:
    """`ok` con las noticias; `partial` (`reason="no_readable_news"`) si el filtro de
    alfabeto (UX-05) se las llevó todas y se sirven las originales. Los errores de la
    fuente suben tipados: la ruta los traduce."""
    # PERF-06: sin caché cada petición costaba 5,6–12,3 s desde Render (Steam +
    # un GET por noticia para el og:image). Misma forma que _inventory_cache.
    now = time.monotonic()
    hit = _news_cache.fresh(count, now)
    if hit is not None:
        return Fetched(hit)

    # UX-05: se piden más de las necesarias porque después se descartan las
    # que no están en alfabeto latino. El tope evita que un `count` alto
    # dispare una petición enorme a Steam.
    fetch_count = min(count * NEWS_OVERFETCH, NEWS_MAX_FETCH)
    newsitems = adapt_news(await news_client.get_news(client, fetch_count))   # forma rara → UnexpectedPayload

    # UX-05: fuera las ilegibles (ruso, chino...), y recorte al `count` pedido.
    # Si el filtro se lo lleva TODO nos quedamos con las originales: más vale una
    # noticia en ruso que un feed vacío.
    readable = [n for n in newsitems if is_readable_news(n)]
    newsitems = (readable or newsitems)[:count]
    images = await asyncio.gather(*[_og_image(client, entry.url or "") for entry in newsitems])
    items = [_map_news_item(item, i, images[i]) for i, item in enumerate(newsitems)]
    _news_cache.put(count, items, now)
    if items and not readable:
        return Fetched(items, "partial", "no_readable_news")
    return Fetched(items)
