"""Cliente único de steamwebapi (CLEAN-06).

Steam, CSFloat y Buff van todos por aquí: los dos últimos son endpoints
`/market/{market}/…` de steamwebapi, no APIs propias. Una función por endpoint;
cada una devuelve el JSON tal cual si el status es 200 y lanza un error tipado
(`steam/errors.py`) si no. **No parsea ni normaliza nada**: eso es de los mappers.

Los timeouts son los de cada llamada antes del cliente; donde no se pasa, manda el
del `httpx.AsyncClient` compartido (10 s, `main.py`). El limiter no se aplica aquí:
lo adquiere el llamador, porque cada uno espera de forma distinta (los crons sin
tope, el chat y /item/history con tope).
"""
import asyncio
import time
from typing import Any

import httpx

from settings import STEAM_API_KEY, STEAM_GAME
from steam.errors import (
    InvalidPayload, QuotaExhausted, RateLimited, SourceTimeout, SourceUnavailable, UpstreamError,
)

STEAM_WEB_API = "https://www.steamwebapi.com/steam/api"
STEAM_MARKET_API = "https://www.steamwebapi.com/market"

_BODY_EXCERPT = 500
_Timeout = Any  # float o httpx.USE_CLIENT_DEFAULT
_FollowRedirects = Any  # bool o httpx.USE_CLIENT_DEFAULT (el tipo no es público)


def steam_auth_headers() -> dict[str, str]:
    """Cabecera de autenticación de steamwebapi (SEC-13).

    La clave va en `X-Api-Key`, nunca en la query (`?key=` es el modo legacy): una
    URL con el secreto acaba en cualquier log que registre URLs, como el INFO de
    httpx o el `str()` de sus excepciones. Solo en llamadas a steamwebapi: el
    cliente compartido también habla con GitHub, frankfurter, Leetify…
    """
    return {"X-Api-Key": STEAM_API_KEY}


# ── Limiter ───────────────────────────────────────────────────────────────────

class _SlidingWindowLimiter:
    """Caps calls to at most `limit` per `window` seconds, process-wide.

    steamwebapi Starter allows 20 req/60s *per endpoint*. _enrich_prices fires
    one csfloat/history call per item (up to 80 for trending) — without this,
    everything past the 20th got HTTP 429 → empty history → priceDelta7d=None →
    "N/A" badges. Callers that exceed the window wait their turn instead of failing.
    ponytail: single global window; if inventory+trending+movers contend heavily,
    split per-endpoint limiters — but they all hit csfloat/history so one is correct.
    """

    def __init__(self, limit: int, window: float):
        self._limit = limit
        self._window = window
        self._calls: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < self._window]
                if len(self._calls) < self._limit:
                    self._calls.append(now)
                    return
                wait = self._window - (now - self._calls[0])
            await asyncio.sleep(max(wait, 0.05))


# 18/60s leaves headroom under the real 20/60s cap for concurrent requests to the
# same endpoint (e.g. /market/prices lookups) sharing the quota.
_history_limiter = _SlidingWindowLimiter(limit=18, window=60.0)


# ── Transporte ────────────────────────────────────────────────────────────────

def _parse_retry_after(value: str | None) -> float | None:
    # ponytail: solo la forma en segundos; la forma fecha-HTTP se trata como ausente.
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None, *,
               timeout: _Timeout = httpx.USE_CLIENT_DEFAULT,
               follow_redirects: _FollowRedirects = httpx.USE_CLIENT_DEFAULT) -> Any:
    try:
        resp = await client.get(url, headers=steam_auth_headers(), params=params,
                                timeout=timeout, follow_redirects=follow_redirects)
    except httpx.TimeoutException as exc:
        raise SourceTimeout(str(exc)) from exc
    except httpx.RequestError as exc:
        raise SourceUnavailable(str(exc)) from exc

    # ponytail: solo 200 es éxito, como comprobaban casi todos los llamadores; un
    # 2xx distinto de steamwebapi no se ha visto nunca.
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as exc:
            raise InvalidPayload(resp.text[:_BODY_EXCERPT]) from exc

    excerpt = resp.text[:_BODY_EXCERPT]
    if resp.status_code == 402:
        raise QuotaExhausted(excerpt)
    if resp.status_code == 429:
        raise RateLimited(_parse_retry_after(resp.headers.get("Retry-After")), excerpt)
    raise UpstreamError(resp.status_code, excerpt)


# ── Endpoints ─────────────────────────────────────────────────────────────────

async def items(client: httpx.AsyncClient, *, select: str, max: int, search: str | None = None,
                sort_by: str | None = None, timeout: _Timeout = 15.0) -> Any:
    """GET /items: búsqueda (`search`) o ranking (`sort_by`). UNA petición sea cual
    sea `max`."""
    params: dict = {"game": "cs2"}
    if search is not None:
        params["search"] = search
    if sort_by is not None:
        params["sort_by"] = sort_by
    params |= {"max": max, "select": select, "format": "json", "production": "1"}
    return await _get(client, f"{STEAM_WEB_API}/items", params, timeout=timeout)


async def item(client: httpx.AsyncClient, market_hash_name: str) -> Any:
    """GET /item: lookup por nombre exacto (price-tick y alertas)."""
    return await _get(
        client, f"{STEAM_WEB_API}/item",
        {"game": "cs2", "market_hash_name": market_hash_name, "format": "json"},
        timeout=20.0, follow_redirects=True,
    )


async def inventory(client: httpx.AsyncClient, steam_id: str) -> Any:
    return await _get(client, f"{STEAM_WEB_API}/inventory", {
        "steam_id": steam_id,
        "game": STEAM_GAME,
        "language": "english",
        "limit": 5000,
        # steamwebapi answers from its own inventory snapshot unless told not to.
        # That snapshot can be days stale, so items acquired since then were
        # invisible to us — including through POST /inventory/refresh, which only
        # bypasses _inventory_cache. Our 23h cache keeps this at ~1 call/day/user,
        # so forcing a live read costs no extra quota.
        "no_cache": 1,
    })


async def profile(client: httpx.AsyncClient, steam_id: str) -> Any:
    return await _get(client, f"{STEAM_WEB_API}/profile", {"id": steam_id})


async def market_index(client: httpx.AsyncClient, *, timeout: _Timeout = httpx.USE_CLIENT_DEFAULT) -> Any:
    return await _get(client, f"{STEAM_WEB_API}/market-index/cs2", {"format": "json"}, timeout=timeout)


async def market_prices(client: httpx.AsyncClient, market: str, params: dict, *,
                        timeout: _Timeout) -> Any:
    """GET /market/{market}/prices: la lista entera (lookup) o un item (`/market/prices`)."""
    return await _get(client, f"{STEAM_MARKET_API}/{market}/prices", params, timeout=timeout)


async def market_history(client: httpx.AsyncClient, market: str, market_hash_name: str,
                         start_date: str, end_date: str, *,
                         timeout: _Timeout = httpx.USE_CLIENT_DEFAULT) -> Any:
    """GET /market/{market}/history (csfloat, buff): fechas y `quantity`."""
    return await _get(client, f"{STEAM_MARKET_API}/{market}/history", {
        "market_hash_name": market_hash_name, "start_date": start_date, "end_date": end_date,
    }, timeout=timeout)


async def legacy_history(client: httpx.AsyncClient, market_hash_name: str, interval: str) -> Any:
    """GET /history: el histórico de Steam (ruta legacy, `interval` y `sold`)."""
    return await _get(client, f"{STEAM_WEB_API}/history", {
        "market_hash_name": market_hash_name, "interval": interval, "format": "json",
    })


async def info_markets(client: httpx.AsyncClient) -> Any:
    return await _get(client, f"{STEAM_WEB_API}/info/markets", timeout=15.0)
