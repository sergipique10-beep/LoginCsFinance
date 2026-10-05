"""Tipo de cambio USD→EUR (UX-08) con caché de 24 h y stale si la fuente cae."""
import logging
import time

import httpx

from steam.cache.market_cache import _fx_cache
from steam.adapters.fx_adapter import adapt_rates
from steam.api import fx_client
from steam.domain.rules import plausible_fx_rate
from steam.errors.handling import DEGRADABLE, reason_of
from steam.domain.models import Fetched

logger = logging.getLogger("uvicorn.error")


def _fx_stale(reason: str) -> Fetched[float | None]:
    # Sin log de degradación: el cliente ya lo ve (`stale` en el cuerpo).
    last = _fx_cache.stale("usdeur")
    return Fetched(last, "stale" if last is not None else "error", reason)


async def fetch_fx_rate(client: httpx.AsyncClient) -> Fetched[float | None]:
    """USD→EUR del BCE, cacheado 24 h. `ok` = tasa fresca; `stale` = la última buena;
    `error` = ninguna (`data` None).

    El backend no convierte nada: solo sirve el numero (UX-08). Si la fuente cae se
    reutiliza el ultimo valor conocido marcado como stale, para que el cliente pueda
    avisar en vez de convertir con una tasa fantasma. Sin valor previo, `data` es None
    y el cliente se queda en USD.
    """
    now = time.monotonic()
    hit = _fx_cache.fresh("usdeur", now)
    if hit is not None:
        return Fetched(hit)
    try:
        rate = adapt_rates(await fx_client.latest_usd_eur(client)).eur   # forma rara → UnexpectedPayload
    except DEGRADABLE as exc:
        logger.warning("[fx] frankfurter failed: %s", reason_of(exc))
        return _fx_stale(reason_of(exc))
    # Un tipo USD/EUR fuera de este rango es un error de la fuente, no un
    # movimiento de mercado: mejor servir el ultimo bueno que corromper precios.
    if not plausible_fx_rate(rate):
        logger.warning("[fx] tasa implausible: %r", rate)
        return _fx_stale("implausible_rate")
    _fx_cache.put("usdeur", float(rate), now)
    logger.info("[fx] USD/EUR = %s", rate)
    return Fetched(float(rate))
