"""Gate de salida por host: GCRA en Valkey, cooldown tras 429 y presupuestos diarios (COST-02).

Todo el estado vive en Valkey, así que varios procesos (worker, scheduler, un script a mano)
comparten el mismo cubo por host: `rl:{host}`. Claves:

  rl:{host}               TAT del GCRA (ms epoch), con TTL
  cool:{host}             duración del cooldown vigente en s, con TTL = esa duración
  budget:{kind}:{yyyymmdd} contador de gasto del día (TTL 3 días)

El reloj es el de Valkey (TIME), no el de cada proceso: dos máquinas con la hora movida
seguirían compartiendo cubo.
"""
import asyncio
import datetime as dt
import logging
import random
from typing import Any

from worker import settings
from worker.settings import HostLimit

log = logging.getLogger(__name__)

# Devuelve 0 si la petición entra (y la consume) o los ms que faltan para que entre.
# ARGV[3] > 0 fija el "ahora" (tests); si no, TIME de Valkey.
GCRA = """
local emission = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
if now <= 0 then
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end
local tat = tonumber(redis.call('GET', KEYS[1])) or now
if tat < now then tat = now end
local new_tat = tat + emission
local allow_at = new_tat - emission * burst
if now < allow_at then return allow_at - now end
redis.call('SET', KEYS[1], new_tat, 'PX', new_tat - now + 1000)
return 0
"""

# 1.er 429: ARGV[1] s. Dentro de un cooldown vigente: el doble del vigente, con tope ARGV[2].
COOLDOWN = """
local cur = tonumber(redis.call('GET', KEYS[1]))
local dur = tonumber(ARGV[1])
if cur and redis.call('TTL', KEYS[1]) > 0 then dur = math.min(cur * 2, tonumber(ARGV[2])) end
redis.call('SET', KEYS[1], dur, 'EX', dur)
return dur
"""

# Gasta 1 del presupuesto si queda; 1 = gastado, 0 = agotado.
SPEND = """
local used = tonumber(redis.call('GET', KEYS[1])) or 0
if used >= tonumber(ARGV[1]) then return 0 end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], 259200)
return 1
"""


class UnknownHost(Exception):
    """Host sin límite declarado en settings: no sale ninguna petición hacia él."""


class HostCoolingDown(Exception):
    def __init__(self, host: str, remaining_s: int) -> None:
        super().__init__(f"{host} en cooldown {remaining_s}s")
        self.host, self.remaining_s = host, remaining_s


class GateTimeout(Exception):
    def __init__(self, host: str, wait_s: float) -> None:
        super().__init__(f"{host}: el gate pide esperar {wait_s:.1f}s, más que el máximo")
        self.host, self.wait_s = host, wait_s


class Gate:
    def __init__(self, redis: Any, limits: dict[str, HostLimit] | None = None,
                 sleep: Any = asyncio.sleep, rand: Any = random.uniform) -> None:
        self.redis = redis
        self.limits = limits if limits is not None else settings.load_limits()
        self._sleep, self._rand = sleep, rand
        self._gcra = redis.register_script(GCRA)
        self._cool = redis.register_script(COOLDOWN)
        self._spend = redis.register_script(SPEND)

    def limit(self, host: str) -> HostLimit:
        try:
            return self.limits[host]
        except KeyError:
            raise UnknownHost(host) from None

    async def try_acquire(self, host: str, now_ms: int = 0) -> int:
        """Una pasada del GCRA: 0 = concedida; si no, ms de espera. No duerme."""
        lim = self.limit(host)
        return int(await self._gcra(keys=[f"rl:{host}"], args=[lim.emission_ms, lim.burst, now_ms]))

    async def cooldown_left(self, host: str) -> int:
        ttl = await self.redis.ttl(f"cool:{host}")
        return max(0, int(ttl))

    async def acquire(self, host: str, max_wait_s: float = 600) -> None:
        """Espera turno en el cubo del host (y el jitter). Un cooldown no se espera: se lanza."""
        lim = self.limit(host)
        left = await self.cooldown_left(host)
        if left:
            raise HostCoolingDown(host, left)
        while (wait_ms := await self.try_acquire(host)) > 0:
            if wait_ms / 1000 > max_wait_s:
                raise GateTimeout(host, wait_ms / 1000)
            await self._sleep(wait_ms / 1000)
        lo, hi = lim.jitter
        if hi > 0:
            await self._sleep(self._rand(lo, hi))

    async def open_cooldown(self, host: str) -> int:
        dur = int(await self._cool(keys=[f"cool:{host}"],
                                   args=[settings.COOLDOWN_FIRST_S, settings.COOLDOWN_MAX_S]))
        log.warning("[gate] cooldown host=%s dur_s=%s", host, dur)
        return dur

    @staticmethod
    def budget_key(kind: str, day: dt.date | None = None) -> str:
        day = day or dt.datetime.now(dt.UTC).date()
        return f"budget:{kind}:{day:%Y%m%d}"

    async def try_spend(self, kind: str, daily_limit: int, day: dt.date | None = None) -> bool:
        return bool(await self._spend(keys=[self.budget_key(kind, day)], args=[daily_limit]))

    async def spent(self, kind: str, day: dt.date | None = None) -> int:
        return int(await self.redis.get(self.budget_key(kind, day)) or 0)
