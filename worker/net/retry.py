"""La única forma de que el worker hable con Internet: gate → breaker → petición → reintentos.

    r = await request(http, "GET", url, gate=gate, breakers=breakers)

Política (COST-02):
- 5xx y errores de transporte: hasta 3 intentos con backoff (stamina) y cuentan para el breaker.
- 429 de Steam (`HostLimit.steam`): abre cooldown en el gate y NO se reintenta.
- 429 sin Retry-After de otra fuente: se reintenta como un 5xx (sin contar para el breaker).
- 429 con Retry-After: no se reintenta aquí; `RateLimited.retry_after` para que el job decida.
- Breaker abierto, cooldown o host sin límite: se lanza tal cual, sin reintento.
"""
from typing import Any
from urllib.parse import urlsplit

import httpx
import stamina

from worker import settings
from worker.net.breaker import Breakers, RateLimited
from worker.net.gate import Gate


class ServerError(Exception):
    def __init__(self, host: str, status: int) -> None:
        super().__init__(f"{host} {status}")
        self.host, self.status = host, status


class RetryableRateLimited(RateLimited):
    """429 sin Retry-After de una fuente que no es Steam."""


def parse_retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # fecha HTTP: se trata como ausente


def retryable(exc: Exception) -> bool:
    return isinstance(exc, ServerError | RetryableRateLimited | httpx.TransportError)


async def request(client: httpx.AsyncClient, method: str, url: str, *, gate: Gate, breakers: Breakers,
                  max_wait_s: float = 600, wait_initial: float = 1.0, **kwargs: Any) -> httpx.Response:
    host = urlsplit(url).hostname or ""
    lim = gate.limit(host)
    breaker = breakers.for_host(host)
    async for attempt in stamina.retry_context(on=retryable, attempts=settings.RETRY_ATTEMPTS,
                                               timeout=None, wait_initial=wait_initial, wait_max=60):
        with attempt:
            await gate.acquire(host, max_wait_s=max_wait_s)
            with breaker.calling():
                r = await client.request(method, url, **kwargs)
                if r.status_code == 429:
                    retry_after = parse_retry_after(r.headers.get("Retry-After"))
                    if lim.steam:
                        await gate.open_cooldown(host)
                        raise RateLimited(host, retry_after)
                    if retry_after is None:
                        raise RetryableRateLimited(host, None)
                    raise RateLimited(host, retry_after)
                if r.status_code >= 500:
                    raise ServerError(host, r.status_code)
            return r
    raise AssertionError("inalcanzable: stamina relanza tras el último intento")
