"""Tipo de cambio USD→EUR (UX-08) con caché de 24 h y stale si la fuente cae (CLEAN-11)."""
import logging
import time

import httpx

from stores import _fx_cache
from steam.clients import fx as fx_client
from steam.errors import SourceTimeout, SourceUnavailable, UpstreamError

logger = logging.getLogger("uvicorn.error")


async def fetch_fx_rate(client: httpx.AsyncClient) -> tuple[float | None, bool]:
    """USD→EUR del BCE, cacheado 24 h. Devuelve (tasa, es_fresca).

    El backend no convierte nada: solo sirve el numero (UX-08). Si la fuente cae se
    reutiliza el ultimo valor conocido marcado como stale, para que el cliente pueda
    avisar en vez de convertir con una tasa fantasma. Sin valor previo → (None, False)
    y el cliente se queda en USD.
    """
    now = time.monotonic()
    hit = _fx_cache.fresh("usdeur", now)
    if hit is not None:
        return hit, True
    try:
        try:
            data = await fx_client.latest_usd_eur(client)
        except (SourceTimeout, SourceUnavailable):
            raise   # red: lo registra el except genérico de abajo, como antes
        except UpstreamError as exc:
            logger.warning("[fx] frankfurter returned %s", exc.status)
            return _fx_cache.stale("usdeur"), False
        rate = (data.get("rates") or {}).get("EUR")
        # Un tipo USD/EUR fuera de este rango es un error de la fuente, no un
        # movimiento de mercado: mejor servir el ultimo bueno que corromper precios.
        if not isinstance(rate, (int, float)) or not 0.5 < rate < 2.0:
            logger.warning("[fx] tasa implausible: %r", rate)
            return _fx_cache.stale("usdeur"), False
        _fx_cache.put("usdeur", float(rate), now)
        logger.info("[fx] USD/EUR = %s", rate)
        return float(rate), True
    except Exception as exc:
        logger.warning("[fx] failed: %s", exc)
        return _fx_cache.stale("usdeur"), False
