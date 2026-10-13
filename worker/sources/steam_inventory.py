"""Inventario de CS2 directo de Valve (COST-07): steamcommunity.com, por el gate.

`inventory/{id}/730/2?l=english&count=2000`, paginado por `last_assetid` mientras haya
`more_items`. Con `Referer` de la página de inventario: sin él Steam responde 400 más a menudo.
Un 403 es inventario privado (no se reintenta); un 429 abre cooldown en el gate (retry.request).
"""
from typing import Any

from worker.net import retry

URL = "https://steamcommunity.com/inventory/{sid}/730/2"
MAX_PAGES = 10   # 20 000 assets: por encima es un bucle, no un inventario


class InventoryPrivate(Exception):
    """403: el usuario tiene el inventario privado."""


class InventoryUnavailable(Exception):
    """400/404/500 sin cuerpo útil: SteamID inválido o inventario que Steam no sirve ahora."""

    def __init__(self, status: int) -> None:
        super().__init__(f"inventario no disponible ({status})")
        self.status = status


async def fetch(state: Any, steam_id: str) -> dict[str, Any]:
    merged: dict[str, Any] = {"assets": [], "descriptions": [], "asset_properties": []}
    headers = {"Referer": f"https://steamcommunity.com/profiles/{steam_id}/inventory/"}
    start = None
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {"l": "english", "count": 2000}
        if start:
            params["start_assetid"] = start
        r = await retry.request(state.http, "GET", URL.format(sid=steam_id), gate=state.gate,
                                breakers=state.breakers, params=params, headers=headers)
        if r.status_code == 403:
            raise InventoryPrivate(steam_id)
        if r.status_code != 200:
            raise InventoryUnavailable(r.status_code)
        page = r.json() or {}
        for k in merged:
            merged[k] += page.get(k) or []
        if not page.get("more_items"):
            break
        start = page.get("last_assetid")
    return merged
