"""Descarga del inventario de CS2 (CLEAN-11), compartida por GET /inventory y la tool
del chat `ver_inventario`. La degradación ante 429/402 (snapshot, reintento: PERF-14)
vive en la ruta, que es quien responde con cabeceras.
"""
import logging

import httpx

from steam.adapters.steam_adapter import adapt_inventory
from steam.api import steam_client
from steam.domain.models import Fetched, SkinCard
from steam.errors import StorageError, UpstreamError
from steam.errors.handling import log_degraded, reason_of
from steam.mappers.item_mapper import _map_item
from steam.services import catalog_service, pricing_service

logger = logging.getLogger("uvicorn.error")

# steamwebapi responde 410/411 a un inventario que no puede leer (perfil borrado,
# inventario sin inicializar). No es un fallo nuestro ni un inventario vacío.
_NO_INVENTORY_STATUSES = (410, 411)


async def fetch_fresh_inventory(client: httpx.AsyncClient, steam_id: str, *, track: bool) -> Fetched[list[SkinCard]]:
    """El inventario mapeado y enriquecido (precios CSFloat/Buff e imágenes).

    - 410/411 → `Fetched([], "error", "http_410")`: no hay inventario que servir, pero
      tampoco es un dato. La ruta sirve el snapshot si lo tiene y NO pisa la caché ni el
      snapshot con `[]` (CAL-13: antes se guardaba el vacío 23 h y en Supabase).
    - El resto de errores del cliente suben tal cual. Un cuerpo que no es lista →
      `UnexpectedPayload` (lo lanza el adapter).
    - `track`: registrar los nombres en `tracked_skins` para la captura diaria. Lo hace
      la ruta y no el chat; es la diferencia que había entre las dos descargas.
    """
    try:
        data = await steam_client.inventory(client, steam_id)
    except UpstreamError as exc:
        if exc.status in _NO_INVENTORY_STATUSES:
            return Fetched([], "error", reason_of(exc))
        raise
    items = [_map_item(item) for item in adapt_inventory(data)]   # cuerpo no lista → UnexpectedPayload
    items = await pricing_service.enrich_market_prices(client, items)
    catalog_service.enrich_images_from_cache(items)

    if track:
        # Auto-registro para la captura de precios (best-effort: nunca romper /inventory)
        try:
            from steam.price_history_repo import register_tracked
            names = [i.get("name") for i in items if i.get("name")]
            if names:
                await register_tracked(names, "inventory")
        except StorageError as exc:
            logger.warning("[price] auto-registro de inventario falló: %s", exc)
            log_degraded("tracked_register", "storage", "empty")

    return Fetched(items)
