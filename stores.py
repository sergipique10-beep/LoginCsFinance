"""
In-memory stores and TTL constants.

WARNING: these stores are only valid for single-worker deployments.
In multi-worker or multi-instance environments, replace with Redis (TTL-native).
TODO: replace _nonces, _auth_codes, _rate_store,
      _profile_cache, _inventory_cache, _market_index_cache and
      _item_history_cache with Redis.
"""
import time
from collections import defaultdict
from datetime import timedelta
from typing import Any

# ── Auth constants ─────────────────────────────────────────────────────────────

NONCE_TTL = 300              # seconds a nonce remains valid
CODE_TTL = 30                # seconds a one-time auth code remains valid
RATE_LIMIT_CALLS = 10        # max requests per window per IP
MARKET_RATE_LIMIT_CALLS = 60  # SEC-03: lecturas /market/* por IP y ventana (Market abre ~6 llamadas)
STATS_RATE_LIMIT_CALLS = 20   # SEC-09: /me/stats* por IP y ventana (el perfil abre 2 llamadas)
ITEM_HISTORY_RATE_LIMIT_CALLS = 60  # SEC-16: /item/history por IP y ventana (cada detalle de skin abre 2)
RATE_LIMIT_WINDOW = 60       # seconds

ACCESS_TOKEN_TTL = timedelta(minutes=30)
REFRESH_TOKEN_TTL = timedelta(days=7)

TOKEN_AUDIENCE = "cs-finance"

# ── Auth stores ────────────────────────────────────────────────────────────────

_nonces: dict[str, tuple[float, str]] = {}       # nonce → (issued_at, redirect_origin)
_auth_codes: dict[str, tuple[str, float]] = {}   # code → (steam_id, expires_at)
# Refresh tokens: persisten en Supabase desde SEC-11, ver auth/refresh_repo.py.
_rate_store: dict[str, list[float]] = defaultdict(list)

# ── Cache constants ────────────────────────────────────────────────────────────

# steamwebapi Starter plan: 20 req/60s per endpoint, 2k/day — cache 23 h to stay well under the daily budget
PROFILE_CACHE_TTL = 82800
INVENTORY_CACHE_TTL = 82800
MARKET_INDEX_CACHE_TTL = 82800
ITEM_HISTORY_CACHE_TTL = 82800
SEARCH_CACHE_TTL = 300       # 5 min — search queries cached briefly to avoid hammering the API
ITEM_PRICE_CACHE_TTL = 300   # 5 min — single-item full lookup (con liquidez) para el detail sheet
MARKET_PRICES_CACHE_TTL = 300  # 5 min — live market prices, updated frequently by steamwebapi
IMAGE_CACHE_TTL = 82800      # 23 h — same budget as other free-plan caches; CDN URLs are stable
MARKET_LOOKUP_CACHE_TTL = 82800  # 23 h — full price list per market (premium endpoint, same daily budget)
MARKET_PROVIDERS_CACHE_TTL = 82800  # 23 h — market list is mostly static
LEETIFY_CACHE_TTL = 300       # 5 min — misma frescura que tenía el staleTime del front (SEC-09)
NEWS_CACHE_TTL = 1800        # 30 min — /news/cs2 llama a Steam + scrapea 5 og:image por petición (PERF-06)
FX_CACHE_TTL = 86400         # 24 h — el BCE publica un tipo al día (UX-08)
# CAL-12: el respaldo de topmovers caduca con el índice; antes se leía como stale sin
# mirar la edad y un movers-tick podía servir un ranking de hace días como "fallback".
TOPMOVERS_RAW_TTL = MARKET_INDEX_CACHE_TTL

INVENTORY_REFRESH_COOLDOWN = 3600  # 1h — manual "force refresh" button, protects shared steamwebapi quota

# Backoff tras un fallo o un vacío: 5 min en vez del TTL largo, para no reintentar en
# cada petición contra una fuente caída (CAL-08, PERF-17) ni guardar 23 h un vacío.
HISTORY_EMPTY_TTL = 300   # histórico de csfloat vacío o fallido (antes _HISTORY_EMPTY_TTL)
IMAGE_FAIL_TTL = 300      # todas las fuentes del catálogo fallaron (CAL-08)
LOOKUP_FAIL_TTL = 300     # lookup de precios por mercado o lista de proveedores (PERF-17)


# ── TtlCache ───────────────────────────────────────────────────────────────────

class TtlCache(dict):
    """Caché `clave → (valor, ts)` con la regla del TTL en un solo sitio (CLEAN-09).

    Sigue siendo un dict, así que lo que lea o escriba `(valor, ts)` a mano funciona.
    `ts` es `time.monotonic()`. Migrar a Redis (CAL-04) es cambiar esta clase.

    - `fresh`: el valor si está dentro del TTL; si no, None. `empty_ttl` acorta el
      TTL de los valores vacíos (un histórico `[]` no vale 23 h).
    - `stale`: el último valor, tenga la edad que tenga (stale-on-error).
    - `mark_failed` / `in_backoff`: caché negativo aparte del valor, para no pisar el
      último dato bueno (PERF-17). Un `put` lo borra.
    - `max_entries`: al pasarse, `put` expulsa las entradas más antiguas. Sin limpieza
      periódica: no hay scheduler (los ticks son crons externos).
    """

    def __init__(self, ttl: float, *, fail_ttl: float = 0, max_entries: int | None = None):
        super().__init__()
        self.ttl = ttl
        self.fail_ttl = fail_ttl
        self.max_entries = max_entries
        self.failed_at: dict[Any, float] = {}
        self.hits = self.misses = self.stale_served = 0

    def fresh(self, key: Any, now: float | None = None, *, empty_ttl: float | None = None) -> Any:
        entry = self.get(key)
        if entry is not None:
            value, ts = entry
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


# ── Cache stores ───────────────────────────────────────────────────────────────

_profile_cache = TtlCache(PROFILE_CACHE_TTL)        # steam_id → perfil
_inventory_cache = TtlCache(INVENTORY_CACHE_TTL)    # steam_id → items
_market_index_cache = TtlCache(MARKET_INDEX_CACHE_TTL)  # tf → índice
# Compartida por /item/history y _fetch_history_for_item, con claves de forma distinta.
_item_history_cache = TtlCache(ITEM_HISTORY_CACHE_TTL)
# "latest" → (gainers, losers): el respaldo de los rankings cuando /items falla (CAL-12).
_topmovers_raw_cache = TtlCache(TOPMOVERS_RAW_TTL)
# Las tres con clave del usuario llevan tope de entradas (Render free: 512 MB).
_search_cache = TtlCache(SEARCH_CACHE_TTL, max_entries=200)        # query → ~30 items (~60 KB)
_item_price_cache = TtlCache(ITEM_PRICE_CACHE_TTL, max_entries=500)  # markethashname.lower() → item
# Sin `name`, el valor es la lista entera de precios de un mercado (MB): tope bajo.
_market_prices_cache = TtlCache(MARKET_PRICES_CACHE_TTL, max_entries=100)
_leetify_cache: dict[tuple[str, str], tuple[Any, float]] = {}  # (steam_id, ruta) → (json, ts)
_news_cache = TtlCache(NEWS_CACHE_TTL)  # count → items
_item_image_cache: dict[str, str] = {}  # markethashname/marketname → image URL
# UX-39: rareza del catálogo estático (ByMykel), poblada junto al caché de imágenes.
_item_rarity_cache: dict[str, tuple[str, str]] = {}  # markethashname → (rareza, color hex sin '#')
# "catalog" → nº de entradas de la última carga buena; fallo total → mark_failed (CAL-08).
_image_cache_meta = TtlCache(IMAGE_CACHE_TTL, fail_ttl=IMAGE_FAIL_TTL)
_market_lookup_cache = TtlCache(MARKET_LOOKUP_CACHE_TTL, fail_ttl=LOOKUP_FAIL_TTL)  # market → {name: price}
_market_providers_cache = TtlCache(MARKET_PROVIDERS_CACHE_TTL, fail_ttl=LOOKUP_FAIL_TTL)  # "providers" → list
# "usdeur" → tasa. Sin TTL al servir el fallback: ver services._fetch_fx_rate
_fx_cache = TtlCache(FX_CACHE_TTL)

_inventory_refresh_cooldown: dict[str, float] = {}  # steam_id → monotonic timestamp of last forced refresh

# Market cap history: ahora persiste en Supabase (Postgres), no en memoria/JSON.
# Ver steam/cap_history_repo.py.
