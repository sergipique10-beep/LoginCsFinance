"""Cliente de Steam News y del og:image de cada noticia (CLEAN-07)."""
import re
from typing import Any

import httpx

from steam.api.http import get_json, get_text

_NEWS_API = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
_CS2_APPID = 730


async def get_news(client: httpx.AsyncClient, count: int) -> Any:
    """Las `count` últimas noticias de CS2, con el timeout del cliente compartido."""
    return await get_json(client, _NEWS_API, {"appid": _CS2_APPID, "count": count, "format": "json"})


_OG_IMAGE = (
    re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+content=["\'](https?://[^"\']+)["\'][^>]+property=["\']og:image["\']', re.IGNORECASE),
)
_OG_TIMEOUT = 4.0


async def fetch_og_image(client: httpx.AsyncClient, url: str) -> str:
    """La imagen og:image de la página de la noticia, o "" si la página no la trae.

    Un fallo de red o un status distinto de 200 **lanza** el error tipado (CLEAN-14):
    antes devolvía "" en silencio y la degradación no tenía motivo. Quien decide que una
    noticia sin imagen se pinta igual es `services/news.py`, que registra el motivo.
    """
    html = await get_text(client, url, timeout=_OG_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
    for pattern in _OG_IMAGE:
        if match := pattern.search(html):
            return match.group(1)
    return ""
