"""`TtlCache`, la caché con contrato de steam/ (CLEAN-09, movida desde stores.py en CLEAN-16).

Sigue siendo un dict de `(valor, ts)` (los tests antiguos lo leen y escriben así) y
centraliza la regla del TTL, el TTL corto de los vacíos, el stale-on-error, el caché
negativo separado del valor (PERF-17) y el tope de entradas. `ts` es `time.monotonic()`.
Migrar a Redis (CAL-04) es cambiar esta clase.

Nada en steam/ ni tools/ compara `x[1]` a mano (guardia en tests/test_cache_policy.py):
se usa `fresh` / `stale` / `lookup`, `put`, y `mark_failed` / `in_backoff`.
"""
import time
from enum import Enum
from typing import Any

from steam.cache.policy import CachePolicy


class CacheState(Enum):
    """Lo que `lookup` encontró: dentro del TTL, caducado, nada, o en backoff tras un fallo."""
    FRESH = "fresh"
    STALE = "stale"
    EMPTY = "empty"
    ERROR = "error"


class TtlCache(dict):
    """Caché `clave → (valor, ts)` con la regla del TTL en un solo sitio (CLEAN-09).

    - `fresh`: el valor si está dentro del TTL; si no, None. `empty_ttl` acorta el
      TTL de los valores vacíos (un histórico `[]` no vale 23 h); por defecto el de la
      política.
    - `stale`: el último valor, tenga la edad que tenga (stale-on-error).
    - `lookup`: `(CacheState, valor)` en una sola llamada (CLEAN-16).
    - `mark_failed` / `in_backoff`: caché negativo aparte del valor, para no pisar el
      último dato bueno (PERF-17). Un `put` lo borra.
    - `max_entries`: al pasarse, `put` expulsa las entradas más antiguas. Sin limpieza
      periódica: no hay scheduler (los ticks son crons externos).
    - `invalidate` / `invalidate_prefix`: por clave o por fuente (`"chat:"`, `"csfloat"`).
    """

    def __init__(self, ttl: float, *, empty_ttl: float | None = None, fail_ttl: float = 0,
                 max_entries: int | None = None, name: str = ""):
        super().__init__()
        self.ttl = ttl
        self.empty_ttl = empty_ttl
        self.fail_ttl = fail_ttl
        self.max_entries = max_entries
        self.name = name
        self.failed_at: dict[Any, float] = {}
        self.hits = self.misses = self.stale_served = 0

    @classmethod
    def from_policy(cls, policy: CachePolicy, *, name: str = "") -> "TtlCache":
        return cls(policy.ttl, empty_ttl=policy.empty_ttl, fail_ttl=policy.fail_ttl,
                   max_entries=policy.max_entries, name=name)

    def fresh(self, key: Any, now: float | None = None, *, empty_ttl: float | None = None) -> Any:
        entry = self.get(key)
        if entry is not None:
            value, ts = entry
            if empty_ttl is None:
                empty_ttl = self.empty_ttl
            ttl = empty_ttl if empty_ttl is not None and not value else self.ttl
            if (time.monotonic() if now is None else now) - ts < ttl:
                self.hits += 1
                return value
        self.misses += 1
        return None

    def stale(self, key: Any) -> Any:
        entry = self.get(key)
        if entry is None:
            return None
        self.stale_served += 1
        return entry[0]

    def lookup(self, key: Any, now: float | None = None) -> tuple[CacheState, Any]:
        """Un solo viaje: `FRESH` con el valor; `ERROR` (en backoff) o `STALE` (caducado)
        con el último valor si lo hay, o None; `EMPTY` sin entrada ni backoff."""
        now = time.monotonic() if now is None else now
        value = self.fresh(key, now)
        if value is not None:
            return CacheState.FRESH, value
        last = self[key][0] if key in self else None
        if self.in_backoff(key, now):
            return CacheState.ERROR, last
        if key in self:
            return CacheState.STALE, last
        return CacheState.EMPTY, None

    def invalidate(self, key: Any) -> None:
        self.pop(key, None)
        self.failed_at.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> int:
        """Borra las claves (str) que empiezan por `prefix`; devuelve cuántas."""
        keys = [k for k in self if isinstance(k, str) and k.startswith(prefix)]
        for k in keys:
            self.invalidate(k)
        return len(keys)

    def put(self, key: Any, value: Any, now: float | None = None) -> None:
        self[key] = (value, time.monotonic() if now is None else now)
        self.failed_at.pop(key, None)
        if self.max_entries is not None:
            while len(self) > self.max_entries:
                del self[min(self, key=lambda k: self[k][1])]

    def mark_failed(self, key: Any, now: float | None = None) -> None:
        self.failed_at[key] = time.monotonic() if now is None else now

    def in_backoff(self, key: Any, now: float | None = None) -> bool:
        failed = self.failed_at.get(key)
        return failed is not None and (time.monotonic() if now is None else now) - failed < self.fail_ttl

    def clear(self) -> None:
        super().clear()
        self.failed_at.clear()
        self.hits = self.misses = self.stale_served = 0

    def stats(self) -> dict[str, int]:
        return {"entries": len(self), "hits": self.hits, "misses": self.misses,
                "stale_served": self.stale_served}
