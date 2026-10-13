"""Clientes de Valkey del worker. Reconectan solos: backoff exponencial y health check.

El asíncrono lo usan el gate y los jobs; el síncrono solo el breaker (pybreaker no es async)
y sin `decode_responses`, que `CircuitRedisStorage` no admite.
"""
from typing import Any

import redis
import redis.asyncio as aredis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

_RETRY_ON = [redis.ConnectionError, redis.TimeoutError]


def _kwargs() -> dict[str, Any]:
    return {"health_check_interval": 30, "socket_keepalive": True, "socket_connect_timeout": 5,
            "retry_on_error": _RETRY_ON}


def async_client(url: str) -> aredis.Redis:
    return aredis.Redis.from_url(url, retry=Retry(ExponentialBackoff(cap=10, base=0.5), 10), **_kwargs())


def sync_client(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, retry=Retry(ExponentialBackoff(cap=10, base=0.5), 10), **_kwargs())
