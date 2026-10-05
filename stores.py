"""
In-memory stores and TTL constants.

WARNING: these stores are only valid for single-worker deployments.
In multi-worker or multi-instance environments, replace with Redis (TTL-native).
TODO: replace _nonces, _auth_codes, _rate_store,
      _profile_cache, _inventory_cache, _market_index_cache and
      _item_history_cache with Redis.
"""
from collections import defaultdict
from datetime import timedelta
from typing import Any

from steam.cache import policy

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
PROFILE_CACHE_TTL = policy.PROFILE.ttl
INVENTORY_CACHE_TTL = policy.INVENTORY.ttl
MARKET_INDEX_CACHE_TTL = policy.MARKET_INDEX.ttl
ITEM_HISTORY_CACHE_TTL = policy.ITEM_HISTORY.ttl
SEARCH_CACHE_TTL = policy.SEARCH.ttl       # 5 min — search queries cached briefly to avoid hammering the API
ITEM_PRICE_CACHE_TTL = policy.ITEM_PRICE.ttl   # 5 min — single-item full lookup (con liquidez) para el detail sheet
MARKET_PRICES_CACHE_TTL = policy.MARKET_PRICES.ttl  # 5 min — live market prices, updated frequently by steamwebapi
IMAGE_CACHE_TTL = policy.IMAGE_CATALOG.ttl      # 23 h — same budget as other free-plan caches; CDN URLs are stable
MARKET_LOOKUP_CACHE_TTL = policy.MARKET_LOOKUP.ttl  # 23 h — full price list per market (premium endpoint, same daily budget)
MARKET_PROVIDERS_CACHE_TTL = policy.MARKET_PROVIDERS.ttl  # 23 h — market list is mostly static
LEETIFY_CACHE_TTL = policy.LEETIFY.ttl       # 5 min — misma frescura que tenía el staleTime del front (SEC-09)
NEWS_CACHE_TTL = policy.NEWS.ttl        # 30 min — /news/cs2 llama a Steam + scrapea 5 og:image por petición (PERF-06)
FX_CACHE_TTL = policy.FX.ttl         # 24 h — el BCE publica un tipo al día (UX-08)
# CAL-12: el respaldo de topmovers caduca con el índice; antes se leía como stale sin
# mirar la edad y un movers-tick podía servir un ranking de hace días como "fallback".
TOPMOVERS_RAW_TTL = MARKET_INDEX_CACHE_TTL

INVENTORY_REFRESH_COOLDOWN = policy.INVENTORY_REFRESH_COOLDOWN.ttl  # 1h — manual "force refresh" button, protects shared steamwebapi quota

# Backoff tras un fallo o un vacío: 5 min en vez del TTL largo, para no reintentar en
# cada petición contra una fuente caída (CAL-08, PERF-17) ni guardar 23 h un vacío.
HISTORY_EMPTY_TTL = policy.ITEM_HISTORY.empty_ttl   # histórico de csfloat vacío o fallido (antes _HISTORY_EMPTY_TTL)
IMAGE_FAIL_TTL = policy.IMAGE_CATALOG.fail_ttl      # todas las fuentes del catálogo fallaron (CAL-08)
LOOKUP_FAIL_TTL = policy.MARKET_LOOKUP.fail_ttl     # lookup de precios por mercado o lista de proveedores (PERF-17)


# ── TtlCache ───────────────────────────────────────────────────────────────────
# Vive en steam/cache/base_cache.py (CLEAN-16); este import es compatibilidad hasta la
# Fase 6. Los TTL de arriba son los de steam/cache/policy.py.
from steam.cache.base_cache import TtlCache  # noqa: E402, F401 — compat, ver arriba


# ── Cache stores ───────────────────────────────────────────────────────────────
# Viven en steam/cache/ (CLEAN-16). Estos alias (mismas instancias) son compatibilidad
# hasta la Fase 6: `from stores import _x` sigue valiendo y conftest los limpia igual.
from steam.cache.history_cache import _item_history_cache, _topmovers_raw_cache  # noqa: E402, F401
from steam.cache.image_cache import catalog_cache  # noqa: E402
from steam.cache.market_cache import (  # noqa: E402, F401
    _fx_cache, _item_price_cache, _market_index_cache, _market_lookup_cache, _market_prices_cache,
    _market_providers_cache, _search_cache,
)
from steam.cache.user_cache import (  # noqa: E402, F401
    _inventory_cache, _inventory_refresh_cooldown, _news_cache, _profile_cache,
)

_leetify_cache: dict[tuple[str, str], tuple[Any, float]] = {}  # (steam_id, ruta) → (json, ts)
_item_image_cache = catalog_cache.images      # markethashname/marketname → image URL
_item_rarity_cache = catalog_cache.rarities   # UX-39: (rareza, color hex sin '#')
_image_cache_meta = catalog_cache.meta        # "catalog" → nº de entradas de la última carga buena

# Market cap history: ahora persiste en Supabase (Postgres), no en memoria/JSON.
# Ver steam/cap_history_repo.py.
