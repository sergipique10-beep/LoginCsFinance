"""Steam Community Market, `priceoverview` (COST-04): precio más bajo, mediana y ventas de 24 h de UN ítem.

Pasa por el gate con el ámbito `steamcommunity.com` (10/min, medido en COST-00 sin 429), distinto
del de `steamcommunity.com/inventory`. Un 429 abre cooldown (retry.request) y el job que llama para.
Steam responde 500 a nombres que no existen en el Market: se reintenta como cualquier 5xx y el
nombre cae del lote; por eso el top-N sale de nombres que ya cotizan en otras fuentes.
"""
from typing import Any

from worker.net import retry

URL = "https://steamcommunity.com/market/priceoverview/"


class PriceUnavailable(Exception):
    def __init__(self, name: str, status: int) -> None:
        super().__init__(f"priceoverview {status} para {name!r}")
        self.name, self.status = name, status


async def priceoverview(state: Any, market_hash_name: str) -> dict[str, Any]:
    params = {"appid": 730, "currency": 1, "market_hash_name": market_hash_name}
    r = await retry.request(state.http, "GET", URL, gate=state.gate, breakers=state.breakers, params=params)
    if r.status_code != 200:
        raise PriceUnavailable(market_hash_name, r.status_code)
    return r.json() or {}
