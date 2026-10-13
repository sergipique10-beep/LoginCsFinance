"""Descarga JSON por la puerta única (gate + breaker + reintentos). Común a todas las fuentes."""
from typing import Any

import httpx

from worker.net import retry


async def get_json(state: Any, url: str, **kwargs: Any) -> Any:
    r = await retry.request(state.http, "GET", url, gate=state.gate, breakers=state.breakers, **kwargs)
    r.raise_for_status()
    return r.json()


async def get_json_etag(state: Any, url: str, etag: str | None, **kwargs: Any) -> tuple[Any, str | None]:
    """(json, etag) o (None, etag) si la fuente responde 304 Not Modified."""
    headers = dict(kwargs.pop("headers", {}) or {})
    if etag:
        headers["If-None-Match"] = etag
    r: httpx.Response = await retry.request(state.http, "GET", url, gate=state.gate, breakers=state.breakers,
                                            headers=headers, **kwargs)
    if r.status_code == 304:
        return None, etag
    r.raise_for_status()
    return r.json(), r.headers.get("ETag")
