"""Gate de salida por host: GCRA en Valkey, cooldown tras 429 y presupuestos diarios (COST-02).

Todo el estado vive en Valkey, así que varios procesos (worker, scheduler, un script a mano)
comparten el mismo cubo por host: `rl:{host}`. Claves:

  rl:{scope}              TAT del GCRA (ms epoch), con TTL
  cool:{scope}            duración del cooldown vigente en s, con TTL = esa duración

`scope` es el host o, si está declarado en settings, `host/prefijo` (`scope(url)`): el inventario
de Steam y priceoverview comparten host pero no límite.
  budget:{kind}:{yyyymmdd} contador de gasto del día (TTL 3 días)

El reloj es el de Valkey (TIME), no el de cada proceso: dos máquinas con la hora movida
seguirían compartiendo cubo.
"""
import asyncio
import datetime as dt
import logging
import random
from typing import Any
from urllib.parse import urlsplit

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

# 1.er 429: ARGV[1] s. Si hubo otro 429 en las últimas 24 h (KEYS[2]): el doble del último, con tope
# ARGV[2]. La memoria va en una clave aparte porque el 429 típico llega en la PRIMERA petición tras
# expirar el cooldown (2026-10-17, ocho veces seguidas): con la memoria en la misma clave nunca doblaba.
COOLDOWN = """
local last = tonumber(redis.call('GET', KEYS[2]))
local dur = tonumber(ARGV[1])
if last then dur = math.min(last * 2, tonumber(ARGV[2])) end
redis.call('SET', KEYS[1], dur, 'EX', dur)
redis.call('SET', KEYS[2], dur, 'EX', 86400)
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

    def scope(self, url: str) -> str:
        """Clave del cubo para una URL: `host/prefijo` si hay uno declarado que case, si no el host."""
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path
        prefixes = (k for k in self.limits if k.startswith(host + "/") and path.startswith(k[len(host):]))
        return max(prefixes, key=len, default=host)

    async def try_acquire(self, host: str, now_ms: int = 0) -> int:
        """Una pasada del GCRA: 0 = concedida; si no, ms de espera. No duerme."""
        lim = self.limit(host)
        return int(await self._gcra(keys=[f"rl:{host}"], args=[lim.emission_ms, lim.burst, now_ms]))

    async def cooldown_left(self, host: str) -> int:
        ttl = await self.redis.ttl(f"cool:{host}")
        return max(0, int(ttl))

    async def acquire(self, host: str, max_wait_s: float = 600) -> None:
        """Espera turno en el cubo del host (y el jitter). Un cooldown no se espera: se lanza,
        salvo que le queden ≤ COOLDOWN_SLACK_S: el cron llega ~1 s antes de que caduque un cooldown
        múltiplo exacto del cron (2026-10-18, 11:00:00 con 1 s) y el tick se perdía entero."""
        lim = self.limit(host)
        left = await self.cooldown_left(host)
        if left > settings.COOLDOWN_SLACK_S:
            raise HostCoolingDown(host, left)
        if left:
            await self._sleep(left + 1)      # +1: el TTL es entero y redondea hacia abajo
        while (wait_ms := await self.try_acquire(host)) > 0:
            if wait_ms / 1000 > max_wait_s:
                raise GateTimeout(host, wait_ms / 1000)
            await self._sleep(wait_ms / 1000)
        lo, hi = lim.jitter
        if hi > 0:
            await self._sleep(self._rand(lo, hi))

    async def open_cooldown(self, host: str) -> int:
        lim = self.limits.get(host)
        first = (lim.cooldown_s if lim else None) or settings.COOLDOWN_FIRST_S
        dur = int(await self._cool(keys=[f"cool:{host}", f"cool:last:{host}"], args=[first, settings.COOLDOWN_MAX_S]))
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
