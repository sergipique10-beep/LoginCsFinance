"""Configuración del worker del VPS (COST-02). Solo entorno; nada importa el backend de Render.

Los límites por host son los del diseño y la tarjeta COST-02. Se ajustan con las cifras de
COST-00 sin tocar código: `GATE_LIMITS` (JSON) sustituye campos de un host concreto, p. ej.
`GATE_LIMITS='{"steamcommunity.com": {"rate": 6}}'`.

Una clave `host/prefijo` declara un ámbito propio para las URL de ese host cuya ruta empiece
por el prefijo (cubo y cooldown aparte; el breaker sigue siendo del host). Medido en COST-00
(2026-10-15): el inventario de steamcommunity.com corta a ~10 peticiones por ventana y
bloquea ≤ 10 min, mientras priceoverview aguanta 10/min sin 429. Mismo host, límites distintos.
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
    cooldown_s: int | None = None   # primer cooldown tras un 429; None = COOLDOWN_FIRST_S (el de priceoverview)

    @property
    def emission_ms(self) -> int:
        return int(1000 * self.per_s / self.rate)


# Hosts sin entrada aquí no pasan el gate: un host nuevo se declara, no se cuela.
DEFAULT_LIMITS: dict[str, HostLimit] = {
    "steamcommunity.com": HostLimit(rate=10, per_s=60, burst=2, reset_timeout=900, steam=True),
    # COST-00: ~10 páginas seguidas → 429 durante ≤ 10 min. 6 por 10 min deja margen.
    # El bloqueo del inventario dura ≤ 10 min (COST-00); el de priceoverview, 2 h. Con el cooldown global de 2 h un
    # solo 429 dejaba sin inventario a todos los usuarios durante 2 h (2026-10-18, 13:37 UTC).
    "steamcommunity.com/inventory": HostLimit(rate=6, per_s=600, burst=2, reset_timeout=900, steam=True, cooldown_s=600),
    "api.steampowered.com": HostLimit(rate=60, per_s=60, reset_timeout=900, steam=True),
    "api.skinport.com": HostLimit(rate=8, per_s=300),
    # Feeds de volcado (COST-03): una descarga cada 15–60 min. Tope conservador, no medido:
    # basta con que un bucle roto no pueda martillear a la fuente.
    "market.csgo.com": HostLimit(rate=4, per_s=60, jitter=(0, 1)),
    "prices.csgotrader.app": HostLimit(rate=4, per_s=60, jitter=(0, 1)),
    "raw.githubusercontent.com": HostLimit(rate=10, per_s=60, jitter=(0, 0)),
    "api.frankfurter.app": HostLimit(rate=4, per_s=60, jitter=(0, 0)),
}

# Cooldown de un host tras un 429 de Steam: 2 h al primero, ×2 por cada 429 dentro, tope 6 h.
# Medido el 2026-10-17 (COST-04): el cubo de priceoverview vuelve a 200 tras ~2 h SIN peticiones; una sonda
# cada 15 min lo mantuvo en 429 más de 7 h. Reintentar antes de las 2 h renueva el bloqueo.
COOLDOWN_FIRST_S = 2 * 3600
COOLDOWN_MAX_S = 6 * 3600
COOLDOWN_SLACK_S = 5          # resto de cooldown que se espera en vez de abortar (el cron llega ~1 s antes)

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
# Steam N pequeño (COST-04). Steam agrupa `priceoverview` por IP + `Accept-Encoding` en cubos con bloqueos
# de horas. La lista son solo alertas activas ∪ inventarios ≤ 7 d (hoy ~20 nombres); el ritmo lo fija la
# cuota medida con `tools/spikes/spike_priceoverview_quota.py` el 2026-10-17:
#   B = 55 peticiones ok en 98 min (1 cada 2 min) hasta el 429; C = ~120 min de silencio hasta el 200
#   cuota_día = B × 1440 / (B_min + C_min) = 55 × 1440 / 218 ≈ 363;  gasto ≤ cuota_día / 2 ≈ 181
# Defectos: 2 nombres cada 30 min = 96/día (≈ 26 % de la cuota) y presupuesto 120 como red de seguridad.
# `STEAM_PRICEOVERVIEW_PER_TICK=0` para el job sin tocar el scheduler (mientras corre el spike).
STEAM_TOPN = int(os.environ.get("STEAM_TOPN", "300"))
STEAM_TOPN_MIN_PRICE = float(os.environ.get("STEAM_TOPN_MIN_PRICE", "10"))
STEAM_PRICEOVERVIEW_PER_TICK = int(os.environ.get("STEAM_PRICEOVERVIEW_PER_TICK", "2"))
STEAM_PRICEOVERVIEW_CRON = os.environ.get("STEAM_PRICEOVERVIEW_CRON", "*/30 * * * *")
STEAM_BUDGET_PRICEOVERVIEW = int(os.environ.get("STEAM_BUDGET_PRICEOVERVIEW", "120"))
# Tabla de Supabase donde compute_reference publica el precio de referencia (sombra hasta COST-06).
MARKET_CURRENT_TABLE = os.environ.get("MARKET_CURRENT_TABLE", "market_current")
# Tablas sombra de COST-05 (rankings, índice y cierre diario). En el corte (COST-06) pasan a las de
# producción cambiando solo estas variables.
MARKET_TRENDING_TABLE = os.environ.get("MARKET_TRENDING_TABLE", "market_trending_own")
MARKET_MOVERS_TABLE = os.environ.get("MARKET_MOVERS_TABLE", "market_movers_own")
MARKET_CAP_HISTORY_TABLE = os.environ.get("MARKET_CAP_HISTORY_TABLE", "market_cap_history_own")
PRECIOS_HISTORICOS_TABLE = os.environ.get("PRECIOS_HISTORICOS_TABLE", "precios_historicos_own")
SHADOW_REPORT_TABLE = os.environ.get("SHADOW_REPORT_TABLE", "shadow_report")   # § 6.3, una fila por día de convivencia
# Valores que nunca pueden salir en un log (worker/logs.py los enmascara).
SECRET_ENV = ("STEAM_WEB_API_KEY", "CSFLOAT_API_KEY", "WAXPEER_API_KEY", "SUPABASE_SERVICE_KEY",
              "VALKEY_PASSWORD", "PG_PASSWORD", "PG_WORKER_PASSWORD")
