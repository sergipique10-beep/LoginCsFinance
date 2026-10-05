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

# PERF-12 — filas que ve el modelo. El inventario entero (~370 filas, ~48 KB)
# pasaba del umbral en que Gemini tarda o da 503 (~20 KB, ver llm/gemini.py).
MAX_ITEMS = 25


def _r(v):
    return round(v, 2) if isinstance(v, (int, float)) else v


def _resumen_inventario(items: list[dict], buscar: str | None = None) -> dict:
    """Totales de todo el inventario + top ``MAX_ITEMS`` por valor, agrupado por nombre.

    ``buscar`` (subcadena, sin mayúsculas) filtra las filas devueltas, no los totales:
    así el modelo puede preguntar por una skin que no entra en el top.
    """
    grupos: dict[str, dict] = {}
    for i in items:
        name = i.get("name") or "?"
        g = grupos.get(name)
        if g is None:
            grupos[name] = g = {
                "name": name,
                "cantidad": 0,
                "priceLatest": _r(i.get("priceLatest")),
                "priceDelta24h": _r(i.get("priceDelta24h")),
                "priceDelta7d": _r(i.get("priceDelta7d")),
                "liquidityScore": i.get("liquidityScore"),
            }
        g["cantidad"] += 1

    def valor(g: dict) -> float:
        return (g["priceLatest"] or 0) * g["cantidad"]

    filas = sorted(grupos.values(), key=valor, reverse=True)
    if buscar:
        q = buscar.lower()
        filas = [g for g in filas if q in g["name"].lower()]

    return {
        "total_items": len(items),
        "distintos": len(grupos),
        "valor_total": _r(sum(valor(g) for g in grupos.values())),
        "sin_precio": sum(1 for i in items if not i.get("priceLatest")),
        "items": filas[:MAX_ITEMS],
        "omitidos": max(0, len(filas) - MAX_ITEMS),
    }


async def _ver_inventario(
    *, steam_id: str, client: httpx.AsyncClient, buscar: str | None = None
) -> dict | list:
    """Devuelve el inventario CS2 del usuario autenticado.

    ``steam_id`` se inyecta desde el JWT en el router — no viene de Gemini.
    """
    from stores import _inventory_cache
    from steam.errors.handling import user_message
    from steam.services import inventory as inventory_service

    import time

    now = time.monotonic()
    hit = _inventory_cache.fresh(steam_id, now)
    if hit is not None:
        items = hit
    else:
        # La misma descarga que GET /inventory, sin registrar en tracked_skins y sin
        # snapshot: así era la del chat antes de unificarlas (CLEAN-11).
        try:
            fetched = await inventory_service.fetch_fresh_inventory(client, steam_id, track=False)
        except Exception as exc:  # noqa: BLE001 — borde del chat: cualquier fallo vuelve al modelo con su motivo (CAL-14), nunca como `[]`
            logger.warning("[tools] ver_inventario falló: %r", exc)
            return {"error": user_message(exc)}
        if fetched.status == "error":   # 410/411 (CAL-13): no hay inventario que leer, y no se cachea
            return {"error": "Steam no tiene inventario de CS2 para este usuario"}
        items = fetched.data
        _inventory_cache.put(steam_id, items, now)

    return _resumen_inventario(items, buscar)


def register_inventory_tools() -> None:
    """Registra las tools de inventario en el registry."""
    register_tool(
        name="ver_inventario",
        description=(
            "Inventario CS2 del usuario autenticado: totales (ítems, valor, sin "
            f"precio) y las {MAX_ITEMS} skins de más valor agrupadas por nombre, con "
            "precio y deltas. Para una skin concreta fuera de ese top, usa `buscar`. "
            "Solo funciona para el usuario logueado."
        ),
        parameters={
            "type": "object",
            "properties": {
                "buscar": {
                    "type": "string",
                    "description": "Parte del nombre de la skin a buscar (p. ej. 'AK-47' o 'Redline').",
                },
            },
        },
        fn=_ver_inventario,
        needs_steam_id=True,
    )
