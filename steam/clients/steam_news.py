"""Cliente de Steam News y del og:image de cada noticia (CLEAN-07)."""
import re
from typing import Any

import httpx

from steam.clients.http import get_json

_NEWS_API = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
_CS2_APPID = 730


async def get_news(client: httpx.AsyncClient, count: int) -> Any:
    """Las `count` últimas noticias de CS2, con el timeout del cliente compartido."""
    return await get_json(client, _NEWS_API, {"appid": _CS2_APPID, "count": count, "format": "json"})


async def fetch_og_image(client: httpx.AsyncClient, url: str) -> str:
    """La imagen og:image de la página de la noticia, o "" ante cualquier fallo: una
    noticia sin imagen se pinta igual."""
    if not url:
        return ""
    try:
        resp = await client.get(
            url, timeout=4.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return ""
        match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)["\']',
            resp.text, re.IGNORECASE,
        ) or re.search(
            r'<meta[^>]+content=["\'](https?://[^"\']+)["\'][^>]+property=["\']og:image["\']',
            resp.text, re.IGNORECASE,
        )
        return match.group(1) if match else ""
    except Exception:
        return ""
