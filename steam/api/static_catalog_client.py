"""Cliente del catálogo estático de ByMykel/CSGO-API (CLEAN-07): imágenes y rareza
de skins, cuchillos, stickers… El bucle por fuentes, el lock (PERF-18), el backoff
(CAL-08) y el registro de claves viven en quien llama (`services._load_static_images`).
"""
from typing import Any

import httpx

from steam.api.http import get_json
from steam.errors import InvalidPayload

_TIMEOUT = 15.0


async def fetch_source(client: httpx.AsyncClient, url: str) -> list[Any]:
    """Un JSON del catálogo: la lista de entradas, o el error tipado."""
    data = await get_json(client, url, timeout=_TIMEOUT)
    if not isinstance(data, list):
        raise InvalidPayload(type(data).__name__)
    return data
