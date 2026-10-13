"""Skinport (COST-03): /v1/items cada 5 min y /v1/sales/history cada 15 min. Exige br."""
from typing import Any

from worker.sources._fetch import get_json

BASE = "https://api.skinport.com/v1"
PARAMS = {"app_id": 730, "currency": "USD"}
HEADERS = {"Accept-Encoding": "br"}   # sin br Skinport responde 406; httpx necesita `brotli`


async def items(state: Any) -> Any:
    return await get_json(state, f"{BASE}/items", params=PARAMS, headers=HEADERS)


async def sales_history(state: Any) -> Any:
    return await get_json(state, f"{BASE}/sales/history", params=PARAMS, headers=HEADERS)
