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

# ── Auth constants ─────────────────────────────────────────────────────────────

NONCE_TTL = 300              # seconds a nonce remains valid
CODE_TTL = 30                # seconds a one-time auth code remains valid
RATE_LIMIT_CALLS = 10        # max requests per window per IP
MARKET_RATE_LIMIT_CALLS = 60  # SEC-03: lecturas /market/* por IP y ventana (Market abre ~6 llamadas)
STATS_RATE_LIMIT_CALLS = 20   # SEC-09: /me/stats* por IP y ventana (el perfil abre 2 llamadas)
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

# steamwebapi.com Starter plan: 20 req/60s per endpoint, 2k/day — cache 23 h to stay well under the daily budget
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

INVENTORY_REFRESH_COOLDOWN = 3600  # 1h — manual "force refresh" button, protects shared steamwebapi quota

# ── Cache stores ───────────────────────────────────────────────────────────────

_profile_cache: dict[str, tuple[dict, float]] = {}
_inventory_cache: dict[str, tuple[list, float]] = {}
_market_index_cache: dict[str, tuple[dict, float]] = {}
_item_history_cache: dict[str, tuple[list, float]] = {}
_topmovers_raw_cache: dict[str, tuple[list, list, float]] = {}  # "latest" → (gainers, losers, ts)
_search_cache: dict[str, tuple[list, float]] = {}
_item_price_cache: dict[str, tuple[Any, float]] = {}  # markethashname.lower() → (ISkinCard, ts)
_market_prices_cache: dict[str, tuple[Any, float]] = {}
_leetify_cache: dict[tuple[str, str], tuple[Any, float]] = {}  # (steam_id, ruta) → (json, ts)
_news_cache: dict[int, tuple[list, float]] = {}  # count → (items, ts)
_item_image_cache: dict[str, str] = {}  # markethashname/marketname → image URL
# UX-39: rareza del catálogo estático (ByMykel), poblada junto al caché de imágenes.
_item_rarity_cache: dict[str, tuple[str, str]] = {}  # markethashname → (rareza, color hex sin '#')
_image_cache_meta: dict[str, float] = {}  # "ts" → monotonic timestamp of last successful population
_market_lookup_cache: dict[str, tuple[dict, float]] = {}  # market → ({name: price}, ts)
_market_providers_cache: dict[str, tuple[list, float]] = {}  # "providers" → (list, ts)
_fx_cache: dict[str, tuple[float, float]] = {}  # "usdeur" → (rate, ts). Sin TTL al servir el fallback: ver services._fetch_fx_rate

_inventory_refresh_cooldown: dict[str, float] = {}  # steam_id → monotonic timestamp of last forced refresh

# Market cap history: ahora persiste en Supabase (Postgres), no en memoria/JSON.
# Ver steam/cap_history_repo.py.
