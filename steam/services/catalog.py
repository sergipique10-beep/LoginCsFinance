"""Catálogo estático de ByMykel (imágenes y rareza) y la caché de imágenes de
steamwebapi (CLEAN-11). La descarga va por `steam/api/static_catalog_client.py`; aquí viven
el registro de claves, el lock de PERF-18 y el backoff de CAL-08.
"""
import asyncio
import logging
import time

import httpx

from stores import IMAGE_FAIL_TTL, _image_cache_meta, _item_image_cache, _item_rarity_cache
from steam.api import static_catalog_client
from steam.errors.handling import log_degraded
from steam.domain.names import catalog_keys_for_skin, image_lookup_candidates, without_souvenir
from steam.errors import InvalidPayload, UpstreamError

logger = logging.getLogger("uvicorn.error")

_STATIC_SKINS_URL     = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json"
_STATIC_STICKERS_URL  = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/stickers.json"
_STATIC_KEYCHAINS_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/keychains.json"
_STATIC_KNIVES_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/knives.json"
_STATIC_CRATES_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/crates.json"
_STATIC_AGENTS_URL    = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/agents.json"
_STATIC_PATCHES_URL   = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/patches.json"


def cache_images(raw_items: list) -> None:
    for raw in raw_items:
        img = raw.get("image", "")
        if not img:
            continue
        for key in (raw.get("markethashname"), raw.get("marketname")):
            if key:
                _item_image_cache[key] = img


def enrich_images_from_cache(items: list) -> list:
    """Rellena `image` desde el catálogo estático. **Muta** los items en sitio y
    devuelve la misma lista (CLEAN-05: mismo contrato que enrich_market_prices)."""
    if not _item_image_cache:
        return items
    for item in items:
        if not item.get("image"):
            candidates = image_lookup_candidates(item.get("name", ""), item.get("itemType"))
            item["image"] = next(
                (img for key in candidates if (img := _item_image_cache.get(key))), "",
            )
    return items


def _rarity_of(item: dict) -> tuple[str, str] | None:
    """(rareza, color hex sin '#') de un ítem del catálogo estático, o None si no la trae."""
    rarity = item.get("rarity") or {}
    name, color = rarity.get("name"), (rarity.get("color") or "").lstrip("#")
    return (name, color) if name and color else None


def _register_keys(keys: list[str], item: dict, image: str) -> None:
    rarity = _rarity_of(item)
    for key in keys:
        _item_image_cache[key] = image
        if rarity:
            _item_rarity_cache[key] = rarity


def _register_skin(item: dict) -> None:
    name = item.get("name", "")
    image = item.get("image", "")
    if not name or not image:
        return
    wears = [w.get("name", "") for w in item.get("wears", []) if w.get("name")]
    _register_keys(catalog_keys_for_skin(name, wears, bool(item.get("stattrak"))), item, image)


def _register_flat(item: dict) -> None:
    image = item.get("image", "")
    if not image:
        return
    keys = [item.get(field, "") for field in ("market_hash_name", "name")]
    _register_keys([k for k in keys if k], item, image)


def rarity_from_cache(name: str) -> tuple[str, str] | None:
    """Rareza de un market_hash_name según el catálogo estático (UX-39). Sirve para
    payloads que no la traen, como topmovers. None si el ítem no está en el catálogo
    (p. ej. los slabs de stickers): quien pinta decide qué hacer sin ella."""
    found = _item_rarity_cache.get(name)
    if not found and (base := without_souvenir(name)) is not None:
        found = _item_rarity_cache.get(base)
    return found


# PERF-18: una sola recarga a la vez. Sin él, N peticiones con la caché caducada
# descargaban N veces los siete JSON (estampida tras despertar Render).
_image_cache_lock = asyncio.Lock()


def _image_cache_fresh(now: float) -> bool:
    # CAL-08: tras un fallo total, backoff corto en vez de reintentar en cada petición.
    return (_image_cache_meta.fresh("catalog", now) is not None
            or _image_cache_meta.in_backoff("catalog", now))


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
    """Una fuente del catálogo falló: se salta y se registra."""
    if isinstance(exc, InvalidPayload):
        logger.warning("[image-cache] %s unexpected format: %s", label, exc.body_excerpt)
    elif isinstance(exc, UpstreamError) and exc.status is not None:
        logger.warning("[image-cache] %s returned %s", label, exc.status)
    else:
        logger.warning("[image-cache] could not fetch %s: %s", label, exc)


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

    total_before = len(_item_image_cache)
    fetched: dict[str, int] = {}

    for label, url in sources_with_wears:
        try:
            data = await static_catalog_client.fetch_source(client, url)
            for item in data:
                _register_skin(item)
            fetched[label] = len(data)
        except Exception as exc:
            _log_catalog_failure(label, exc)

    for label, url in sources_flat:
        try:
            data = await static_catalog_client.fetch_source(client, url)
            for item in data:
                _register_flat(item)
            fetched[label] = len(data)
        except Exception as exc:
            _log_catalog_failure(label, exc)

    # CAL-08: "catalog" significa "última carga buena" (stores.py). Si no cargó ninguna
    # fuente (GitHub caído en el arranque de Render), no se estampa: se reintenta
    # pasado IMAGE_FAIL_TTL en vez de pasar 23 h con `image: ""`.
    if not fetched:
        _image_cache_meta.mark_failed("catalog", now)
        logger.warning("[image-cache] all sources failed; retry in %ds", IMAGE_FAIL_TTL)
        log_degraded("catalog", "all_sources_failed", "empty")
        return
    _image_cache_meta.put("catalog", len(_item_image_cache), now)
    logger.info(
        "[image-cache] loaded %d total entries (%+d new) — sources: %s",
        len(_item_image_cache),
        len(_item_image_cache) - total_before,
        fetched,
    )
