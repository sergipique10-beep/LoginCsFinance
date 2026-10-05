"""Cliente de Buff163 vía steamwebapi (CLEAN-14): los endpoints `/market/buff/…`.
No es una API propia: comparte transporte, clave y errores con `steam_client`. Existe
para que los services pidan «el histórico de Buff163» sin pasar el nombre del mercado
como string; `steam.api.MARKET_CLIENTS` elige el módulo por mercado.
"""
from typing import Any

import httpx

from steam.api import steam_client
from steam.api.http import Timeout

MARKET = "buff"


async def history(client: httpx.AsyncClient, market_hash_name: str, start_date: str, end_date: str, *,
                  timeout: Timeout = httpx.USE_CLIENT_DEFAULT) -> Any:
    """GET /market/buff/history: fechas y `quantity`."""
    return await steam_client.market_history(client, MARKET, market_hash_name, start_date, end_date,
                                             timeout=timeout)


async def prices(client: httpx.AsyncClient, params: dict, *, timeout: Timeout) -> Any:
    """GET /market/buff/prices: la lista entera (lookup) o un item."""
    return await steam_client.market_prices(client, MARKET, params, timeout=timeout)
