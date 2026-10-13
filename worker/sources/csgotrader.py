"""csgotrader buff163.json (COST-03): cada 60 min con If-None-Match (ETag en Valkey)."""
from typing import Any

from worker.sources._fetch import get_json_etag

URL = "https://prices.csgotrader.app/latest/buff163.json"
ETAG_KEY = "etag:csgotrader:buff163"


async def buff163(state: Any) -> Any:
    """JSON nuevo, o None si no cambió desde la última descarga."""
    etag = await state.redis.get(ETAG_KEY)
    data, new_etag = await get_json_etag(state, URL, etag.decode() if isinstance(etag, bytes) else etag)
    if data is not None and new_etag:
        await state.redis.set(ETAG_KEY, new_etag)
    return data
