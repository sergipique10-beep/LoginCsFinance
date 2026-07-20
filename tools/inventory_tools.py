"""Tools de inventario para el orquestador de Sharky.

La tool ``ver_inventario`` necesita ``steam_id`` para saber QUÉ inventario
consultar. Este parámetro se inyecta ocultamente server-side — Gemini nunca
lo ve ni lo controla.
"""

from __future__ import annotations

import logging

import httpx

from tools.registry import register_tool

logger = logging.getLogger("uvicorn.error")


async def _ver_inventario(*, steam_id: str, client: httpx.AsyncClient) -> list[dict]:
    """Devuelve el inventario CS2 del usuario autenticado.

    ``steam_id`` se inyecta desde el JWT en el router — no viene de Gemini.
    """
    from stores import _inventory_cache, INVENTORY_CACHE_TTL
    from steam.services import (
        STEAM_WEB_API,
        STEAM_MARKET_API,
        _enrich_market_prices,
        _enrich_images_from_cache,
    )
    from steam.mappers import _map_item
    from settings import STEAM_API_KEY, STEAM_GAME

    import time

    now = time.monotonic()
    cached = _inventory_cache.get(steam_id)
    if cached and now - cached[1] < INVENTORY_CACHE_TTL:
        items = cached[0]
    else:
        try:
            resp = await client.get(
                f"{STEAM_WEB_API}/inventory",
                params={
                    "steam_id": steam_id,
                    "game": STEAM_GAME,
                    "key": STEAM_API_KEY,
                    "language": "english",
                    "limit": 5000,
                    "no_cache": 1,
                },
            )
        except Exception as exc:
            logger.warning("[tools] ver_inventario falló: %s", exc)
            return []

        if resp.status_code != 200:
            logger.warning("[tools] ver_inventario steamwebapi → %s", resp.status_code)
            return []

        data = resp.json()
        if not isinstance(data, list):
            return []

        items = [_map_item(item) for item in data]
        items = await _enrich_market_prices(client, items)
        _enrich_images_from_cache(items)
        _inventory_cache[steam_id] = (items, now)

    # Devolver forma reducida para no sobrecargar el contexto de Gemini
    return [
        {
            "name": i.get("name"),
            "priceLatest": i.get("priceLatest"),
            "priceDelta24h": i.get("priceDelta24h"),
            "priceDelta7d": i.get("priceDelta7d"),
            "liquidityScore": i.get("liquidityScore"),
        }
        for i in items
    ]


def register_inventory_tools() -> None:
    """Registra las tools de inventario en el registry."""
    register_tool(
        name="ver_inventario",
        description=(
            "Muestra el inventario CS2 del usuario autenticado con precios actuales "
            "y deltas de precio. Solo funciona para el usuario logueado."
        ),
        parameters={"type": "object", "properties": {}},
        fn=_ver_inventario,
        needs_steam_id=True,
    )
