"""Configuración del worker del VPS (COST-02). Solo entorno; nada importa el backend de Render.

Los límites por host son los del diseño y la tarjeta COST-02. Se ajustan con las cifras de
COST-00 sin tocar código: `GATE_LIMITS` (JSON) sustituye campos de un host concreto, p. ej.
`GATE_LIMITS='{"steamcommunity.com": {"rate": 6}}'`.
"""
import json
import os
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class HostLimit:
    rate: int                    # peticiones…
    per_s: float                 # …por esta ventana
    burst: int = 1               # cuántas pueden salir seguidas desde el cubo vacío
    jitter: tuple[float, float] = (0.5, 2.0)   # espera extra aleatoria tras pasar el gate
    reset_timeout: int = 300     # segundos que el breaker se queda abierto
    steam: bool = False          # un 429 abre cooldown y no se reintenta

    @property
    def emission_ms(self) -> int:
        return int(1000 * self.per_s / self.rate)


# Hosts sin entrada aquí no pasan el gate: un host nuevo se declara, no se cuela.
DEFAULT_LIMITS: dict[str, HostLimit] = {
    "steamcommunity.com": HostLimit(rate=10, per_s=60, burst=2, reset_timeout=900, steam=True),
    "api.steampowered.com": HostLimit(rate=60, per_s=60, reset_timeout=900, steam=True),
    "api.skinport.com": HostLimit(rate=8, per_s=300),
    # Feeds de volcado (COST-03): una descarga cada 15–60 min. Tope conservador, no medido:
    # basta con que un bucle roto no pueda martillear a la fuente.
    "market.csgo.com": HostLimit(rate=4, per_s=60, jitter=(0, 1)),
    "prices.csgotrader.app": HostLimit(rate=4, per_s=60, jitter=(0, 1)),
    "raw.githubusercontent.com": HostLimit(rate=10, per_s=60, jitter=(0, 0)),
    "api.frankfurter.app": HostLimit(rate=4, per_s=60, jitter=(0, 0)),
}

# Cooldown de un host tras un 429 de Steam: 1 h al primero, ×2 por cada 429 dentro, tope 6 h.
COOLDOWN_FIRST_S = 3600
COOLDOWN_MAX_S = 6 * 3600

BREAKER_FAIL_MAX = 5
RETRY_ATTEMPTS = 3


def load_limits(raw: str | None = None) -> dict[str, HostLimit]:
    raw = os.environ.get("GATE_LIMITS", "") if raw is None else raw
    limits = dict(DEFAULT_LIMITS)
    for host, fields in (json.loads(raw) if raw.strip() else {}).items():
        if "jitter" in fields:
            fields["jitter"] = tuple(fields["jitter"])
        limits[host] = replace(limits[host], **fields) if host in limits else HostLimit(**fields)
    return limits


def kuma_tokens(raw: str | None = None) -> dict[str, str]:
    """KUMA_PUSH_TOKENS='noop=abc,market-sync=def' → {job: token}."""
    raw = os.environ.get("KUMA_PUSH_TOKENS", "") if raw is None else raw
    pairs = (p.split("=", 1) for p in raw.split(",") if "=" in p)
    return {k.strip(): v.strip() for k, v in pairs}


VALKEY_URL = os.environ.get("VALKEY_URL", "redis://localhost:6379/0")
PG_DSN = os.environ.get("PG_DSN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
KUMA_URL = os.environ.get("KUMA_URL", "http://uptime-kuma:3001")
# Moneda de buff163.json de csgotrader: USD o CNY. La resuelve el spike COST-00; vacía = el job
# de csgotrader no corre (no se supone una moneda).
BUFF_CURRENCY = os.environ.get("BUFF_CURRENCY", "").strip().upper()
# Tabla de Supabase donde compute_reference publica el precio de referencia (sombra hasta COST-06).
MARKET_CURRENT_TABLE = os.environ.get("MARKET_CURRENT_TABLE", "market_current")
# Valores que nunca pueden salir en un log (worker/logs.py los enmascara).
SECRET_ENV = ("STEAM_WEB_API_KEY", "CSFLOAT_API_KEY", "WAXPEER_API_KEY", "SUPABASE_SERVICE_KEY",
              "VALKEY_PASSWORD", "PG_PASSWORD", "PG_WORKER_PASSWORD")
