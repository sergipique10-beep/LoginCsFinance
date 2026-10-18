"""Skinport (COST-03): /v1/items y /v1/sales/history cada 15 min. Exige br.

Items iba cada 5 min (lo que documenta Skinport) y aun así baneaba /v1/items 1 h tras ~20 peticiones
seguidas (12 baneos en 48 h, 2026-10-18): hay un presupuesto no documentado de ~20/2 h por endpoint.
"""
from typing import Any

from worker.sources._fetch import get_json

BASE = "https://api.skinport.com/v1"
PARAMS = {"app_id": 730, "currency": "USD"}
HEADERS = {"Accept-Encoding": "br"}   # sin br Skinport responde 406; httpx necesita `brotli`


async def items(state: Any) -> Any:
    return await get_json(state, f"{BASE}/items", params=PARAMS, headers=HEADERS)


async def sales_history(state: Any) -> Any:
    return await get_json(state, f"{BASE}/sales/history", params=PARAMS, headers=HEADERS)
