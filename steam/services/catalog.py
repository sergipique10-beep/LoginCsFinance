"""Catálogo estático de ByMykel (imágenes y rareza) y la caché de imágenes de
steamwebapi (CLEAN-11). La descarga va por `steam/api/static_catalog_client.py`; aquí viven
el registro de claves, el lock de PERF-18 y el backoff de CAL-08.
"""
import asyncio
import logging
import time
from collections.abc import Sequence

import httpx

from steam.cache.image_cache import catalog_cache
from steam.adapters.static_catalog_adapter import adapt_catalog_source
from steam.api import static_catalog_client
from steam.errors.handling import DEGRADABLE, log_degraded, reason_of
from steam.domain.models import CatalogEntry, SteamItem
from steam.domain.names import catalog_keys_for_skin, image_lookup_candidates, without_souvenir

logger = logging.getLogger("uvicorn.error")

_STATIC_SKINS_URL     = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json"
_STATIC_STICKERS_URL  = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/stickers.json"
_STATIC_KEYCHAINS_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/keychains.json"
_STATIC_KNIVES_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/knives.json"
_STATIC_CRATES_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/crates.json"
_STATIC_AGENTS_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/agents.json"
_STATIC_PATCHES_URL   = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/patches.json"


def cache_images(items: Sequence[SteamItem]) -> None:
    """Guarda la imagen que trae steamwebapi bajo ambos nombres del item."""
    for item in items:
        if not item.image:
            continue
        catalog_cache.register([k for k in (item.market_hash_name, item.market_name) if k], item.image)


def enrich_images_from_cache(items: list) -> list:
    """Rellena `image` desde el catálogo estático. **Muta** los items en sitio y
    devuelve la misma lista (CLEAN-05: mismo contrato que enrich_market_prices)."""
    if not catalog_cache:
        return items
    for item in items:
        if not item.get("image"):
            item["image"] = catalog_cache.image_for(
                image_lookup_candidates(item.get("name", ""), item.get("itemType")))
    return items


def _rarity_of(entry: CatalogEntry) -> tuple[str, str] | None:
    """(rareza, color hex sin '#') de una entrada del catálogo, o None si no la trae."""
    return (entry.rarity_name, entry.rarity_color) if entry.rarity_name and entry.rarity_color else None


def _register_keys(keys: list[str], entry: CatalogEntry, image: str) -> None:
    catalog_cache.register(keys, image, _rarity_of(entry))


def _register_skin(entry: CatalogEntry) -> None:
    if not entry.name or not entry.image:
        return
    _register_keys(catalog_keys_for_skin(entry.name, list(entry.wears), entry.stattrak), entry, entry.image)


def _register_flat(entry: CatalogEntry) -> None:
    if not entry.image:
        return
    keys = [k for k in (entry.market_hash_name, entry.name) if k]
    _register_keys(keys, entry, entry.image)


def rarity_from_cache(name: str) -> tuple[str, str] | None:
    """Rareza de un market_hash_name según el catálogo estático (UX-39). Sirve para
    payloads que no la traen, como topmovers. None si el ítem no está en el catálogo
    (p. ej. los slabs de stickers): quien pinta decide qué hacer sin ella."""
    found = catalog_cache.rarity_for(name)
    if not found and (base := without_souvenir(name)) is not None:
        found = catalog_cache.rarity_for(base)
    return found


# PERF-18: una sola recarga a la vez. Sin él, N peticiones con la caché caducada
# descargaban N veces los siete JSON (estampida tras despertar Render).
_image_cache_lock = asyncio.Lock()


def _image_cache_fresh(now: float) -> bool:
    # CAL-08: tras un fallo total, backoff corto en vez de reintentar en cada petición.
    return catalog_cache.is_fresh_or_backoff(now)


async def fetch_static_images(client: httpx.AsyncClient) -> None:
    if _image_cache_fresh(time.monotonic()):
        return
    async with _image_cache_lock:
        # Doble comprobación: quien tenía el lock pudo recargar mientras esperábamos.
        # Una cancelación a media descarga suelta el lock (async with) y no deja
        # `ts` ni `failed_ts`, que solo se escriben al final: el siguiente reintenta.
        now = time.monotonic()
        if _image_cache_fresh(now):
            return
        await _load_static_images(client, now)


def _log_catalog_failure(label: str, exc: Exception) -> None:
    """Una fuente del catálogo falló: se salta (las demás cargan) y deja su línea. Antes
    el fallo parcial no quedaba registrado como degradación (CLEAN-15)."""
    logger.warning("[image-cache] could not load %s (%s): %s", label, reason_of(exc), exc)
    log_degraded("catalog", reason_of(exc), "empty")


async def _load_static_images(client: httpx.AsyncClient, now: float) -> None:
    sources_with_wears = [
        ("skins",  _STATIC_SKINS_URL),
        ("knives", _STATIC_KNIVES_URL),
    ]
    sources_flat = [
        ("stickers",  _STATIC_STICKERS_URL),
        ("keychains", _STATIC_KEYCHAINS_URL),
        ("crates",    _STATIC_CRATES_URL),
        ("agents",    _STATIC_AGENTS_URL),
        ("patches",   _STATIC_PATCHES_URL),
    ]

    total_before = len(catalog_cache)
    fetched: dict[str, int] = {}

    for label, url in sources_with_wears:
        try:
            data = adapt_catalog_source(await static_catalog_client.fetch_source(client, url), label=label)
            for entry in data:
                _register_skin(entry)
            fetched[label] = len(data)
        except DEGRADABLE as exc:
            _log_catalog_failure(label, exc)

    for label, url in sources_flat:
        try:
            data = adapt_catalog_source(await static_catalog_client.fetch_source(client, url), label=label)
            for entry in data:
                _register_flat(entry)
            fetched[label] = len(data)
        except DEGRADABLE as exc:
            _log_catalog_failure(label, exc)

    # CAL-08: "catalog" significa "última carga buena" (stores.py). Si no cargó ninguna
    # fuente (GitHub caído en el arranque de Render), no se estampa: se reintenta
    # pasado IMAGE_FAIL_TTL en vez de pasar 23 h con `image: ""`.
    if not fetched:
        catalog_cache.mark_failed(now)
        logger.warning("[image-cache] all sources failed; retry in %ds", catalog_cache.meta.fail_ttl)
        log_degraded("catalog", "all_sources_failed", "empty")
        return
    catalog_cache.mark_loaded(now)
    logger.info(
        "[image-cache] loaded %d total entries (%+d new) — sources: %s",
        len(catalog_cache),
        len(catalog_cache) - total_before,
        fetched,
    )
