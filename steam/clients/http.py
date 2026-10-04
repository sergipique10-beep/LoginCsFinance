"""Transporte común de los clientes de steam/ (CLEAN-07): un GET que devuelve el JSON
del 200 o lanza el error tipado de `steam/errors.py`. Lo usan steamwebapi, el
catálogo de ByMykel, frankfurter y Steam News; cada uno pone su URL y su timeout.
"""
from typing import Any

import httpx

from steam.errors import (
    InvalidPayload, QuotaExhausted, RateLimited, SourceTimeout, SourceUnavailable, UpstreamError,
)

BODY_EXCERPT = 500
# float/bool o httpx.USE_CLIENT_DEFAULT: el tipo de este último no es público.
Timeout = Any
FollowRedirects = Any


def parse_retry_after(value: str | None) -> float | None:
    # ponytail: solo la forma en segundos; la forma fecha-HTTP se trata como ausente.
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


async def get_json(client: httpx.AsyncClient, url: str, params: dict | None = None, *,
                   headers: dict | None = None, timeout: Timeout = httpx.USE_CLIENT_DEFAULT,
                   follow_redirects: FollowRedirects = httpx.USE_CLIENT_DEFAULT) -> Any:
    try:
        resp = await client.get(url, headers=headers, params=params,
                                timeout=timeout, follow_redirects=follow_redirects)
    except httpx.TimeoutException as exc:
        raise SourceTimeout(str(exc)) from exc
    except httpx.RequestError as exc:
        raise SourceUnavailable(str(exc)) from exc

    # ponytail: solo 200 es éxito, como comprobaban casi todos los llamadores; un
    # 2xx distinto no se ha visto nunca en estas fuentes.
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:
            raise InvalidPayload(resp.text[:BODY_EXCERPT]) from exc

    excerpt = resp.text[:BODY_EXCERPT]
    if resp.status_code == 402:
        raise QuotaExhausted(excerpt)
    if resp.status_code == 429:
        raise RateLimited(parse_retry_after(resp.headers.get("Retry-After")), excerpt)
    raise UpstreamError(resp.status_code, excerpt)
