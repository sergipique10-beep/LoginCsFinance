"""Descarga del inventario de CS2 (CLEAN-11), compartida por GET /inventory y la tool
del chat `ver_inventario`. La degradación ante 429/402 (snapshot, reintento: PERF-14)
vive en la ruta, que es quien responde con cabeceras.
"""
import logging

import httpx

from steam.adapters.steam_adapter import adapt_inventory
from steam.api import steam_client
from steam.domain.models import SkinCard
from steam.mappers.item_mapper import _map_item
from steam.services import catalog, pricing

logger = logging.getLogger("uvicorn.error")


async def fetch_fresh_inventory(client: httpx.AsyncClient, steam_id: str, *, track: bool) -> list[SkinCard]:
    """El inventario mapeado y enriquecido (precios CSFloat/Buff e imágenes).

    - Los errores del cliente suben tal cual (la ruta trata el 410/411 como inventario
      vacío y el chat como fallo; CAL-13). Un cuerpo que no es lista → `UnexpectedPayload`
      (lo lanza el adapter).
    - `track`: registrar los nombres en `tracked_skins` para la captura diaria. Lo hace
      la ruta y no el chat; es la diferencia que había entre las dos descargas.
    """
    data = await steam_client.inventory(client, steam_id)
    items = [_map_item(item) for item in adapt_inventory(data)]   # cuerpo no lista → UnexpectedPayload
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
