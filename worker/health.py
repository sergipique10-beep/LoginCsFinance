"""Push de salud a Uptime Kuma al final de cada job (monitores tipo push, COST-02/08).

Un job sin token en KUMA_PUSH_TOKENS no empuja nada (se registra a DEBUG): así un job nuevo
no rompe por faltar su monitor. Kuma está en la red interna: no pasa por el gate.
"""
import logging

import httpx

from worker import settings

log = logging.getLogger(__name__)


async def push(client: httpx.AsyncClient, job: str, ok: bool, msg: str = "", ping_ms: float | None = None,
               tokens: dict[str, str] | None = None, base_url: str | None = None) -> bool:
    token = (tokens if tokens is not None else settings.kuma_tokens()).get(job)
    if not token:
        log.debug("[health] job=%s sin token de Kuma", job)
        return False
    params: dict[str, str] = {"status": "up" if ok else "down", "msg": msg[:200]}
    if ping_ms is not None:
        params["ping"] = str(round(ping_ms))
    try:
        r = await client.get(f"{base_url or settings.KUMA_URL}/api/push/{token}", params=params, timeout=10)
    except httpx.HTTPError as e:
        log.warning("[health] job=%s push fallido: %r", job, e)
        return False
    return r.status_code == 200
