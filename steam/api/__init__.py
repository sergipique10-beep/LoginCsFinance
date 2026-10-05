"""Clientes HTTP de las fuentes externas de steam/ (ex `steam/clients/`, CLEAN-14).

Uno por fuente, sin caché, sin fallback y sin normalizar (CLEAN-06/07): devuelven el
JSON del 200 o lanzan el error tipado de `steam/errors/`. `MARKET_CLIENTS` da el módulo
de cada mercado con histórico (`HISTORY_MARKETS` del dominio) por su nombre.
"""
from types import ModuleType

from steam.api import buff_client, csfloat_client

MARKET_CLIENTS: dict[str, ModuleType] = {
    csfloat_client.MARKET: csfloat_client,
    buff_client.MARKET: buff_client,
}
