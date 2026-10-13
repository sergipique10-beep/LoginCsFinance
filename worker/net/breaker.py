"""Circuit breaker por host con estado en Valkey (COST-02).

pybreaker + `CircuitRedisStorage(namespace="cb:{host}")`: todos los procesos ven el mismo
estado. 5 fallos seguidos abren el circuito; tras `reset_timeout` (300 s, Steam 900) pasa a
half-open y la siguiente petición decide. Un 429 NO es un fallo del host (lo gestiona el
cooldown del gate), así que los errores de límite están en `exclude`.

`CircuitRedisStorage` usa el cliente síncrono de redis: son operaciones O(1) contra el Valkey
local, aceptables dentro del bucle de eventos.
"""
import logging
from typing import Any

import pybreaker

from worker import settings
from worker.settings import HostLimit

log = logging.getLogger(__name__)


class RateLimited(Exception):
    """429 de la fuente. No cuenta como caída del host."""

    def __init__(self, host: str, retry_after: float | None) -> None:
        super().__init__(f"{host} 429 retry_after={retry_after}")
        self.host, self.retry_after = host, retry_after


class LogListener(pybreaker.CircuitBreakerListener):
    def state_change(self, cb: pybreaker.CircuitBreaker, old_state: Any, new_state: Any) -> None:
        old = getattr(old_state, "name", old_state)
        log.warning("[breaker] host=%s %s -> %s fails=%s", cb.name, old, new_state.name, cb.fail_counter)


class Breakers:
    def __init__(self, sync_redis: Any, limits: dict[str, HostLimit] | None = None) -> None:
        self.redis = sync_redis
        self.limits = limits if limits is not None else settings.load_limits()
        self._by_host: dict[str, pybreaker.CircuitBreaker] = {}

    def for_host(self, host: str) -> pybreaker.CircuitBreaker:
        cb = self._by_host.get(host)
        if cb is None:
            lim = self.limits.get(host)
            storage = pybreaker.CircuitRedisStorage(pybreaker.STATE_CLOSED, self.redis, namespace=f"cb:{host}")
            cb = pybreaker.CircuitBreaker(
                fail_max=settings.BREAKER_FAIL_MAX,
                reset_timeout=lim.reset_timeout if lim else 300,
                state_storage=storage, listeners=[LogListener()], exclude=[RateLimited], name=host)
            self._by_host[host] = cb
        return cb
