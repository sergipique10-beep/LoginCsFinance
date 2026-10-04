"""Descarga del inventario de CS2 (CLEAN-11), compartida por GET /inventory y la tool
del chat `ver_inventario`. La degradación ante 429/402 (snapshot, reintento: PERF-14)
vive en la ruta, que es quien responde con cabeceras.
"""
import logging

import httpx

from steam.api import steam_client
from steam.domain.models import SkinCard
from steam.errors import UNEXPECTED_FORMAT, UnexpectedPayload
from steam.mappers.items import _map_item
from steam.services import catalog, pricing

logger = logging.getLogger("uvicorn.error")


async def fetch_fresh_inventory(client: httpx.AsyncClient, steam_id: str, *, track: bool) -> list[SkinCard]:
    """El inventario mapeado y enriquecido (precios CSFloat/Buff e imágenes).

    - Los errores del cliente suben tal cual (la ruta trata el 410/411 como inventario
      vacío y el chat como fallo; CAL-13). Un cuerpo que no es lista → `UnexpectedPayload`.
    - `track`: registrar los nombres en `tracked_skins` para la captura diaria. Lo hace
      la ruta y no el chat; es la diferencia que había entre las dos descargas.
    """
    data = await steam_client.inventory(client, steam_id)
    if not isinstance(data, list):
        logger.error("steamwebapi /inventory unexpected format: %.500s", data)
        raise UnexpectedPayload(UNEXPECTED_FORMAT)

    items = [_map_item(item) for item in data]
    items = await pricing.enrich_market_prices(client, items)
    catalog.enrich_images_from_cache(items)

    if track:
        # Auto-registro para la captura de precios (best-effort: nunca romper /inventory)
        try:
            from steam.price_history_repo import register_tracked
            names = [i.get("name") for i in items if i.get("name")]
            if names:
                await register_tracked(names, "inventory")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[price] auto-registro de inventario falló: %s", exc)

    return items
