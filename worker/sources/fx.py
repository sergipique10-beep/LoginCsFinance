"""Tipo de cambio diario USD→CNY (frankfurter, BCE). Solo hace falta si Buff viene en CNY."""
from typing import Any

from worker.sources._fetch import get_json

URL = "https://api.frankfurter.app/latest"


async def usd_cny(state: Any) -> Any:
    return await get_json(state, URL, params={"from": "USD", "to": "CNY"})
