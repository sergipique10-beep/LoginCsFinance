"""market.csgo.com (COST-03): volcado de precios en USD, cada 15 min."""
from typing import Any

from worker.sources._fetch import get_json

URL = "https://market.csgo.com/api/v2/prices/USD.json"


async def prices(state: Any) -> Any:
    return await get_json(state, URL)
