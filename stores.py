"""
In-memory stores and TTL constants.

WARNING: these stores are only valid for single-worker deployments.
In multi-worker or multi-instance environments, replace with Redis (TTL-native).
TODO: replace _nonces, _auth_codes, _refresh_store, _rate_store,
      _profile_cache, _inventory_cache, _market_index_cache and
      _item_history_cache with Redis.
"""
from collections import defaultdict
from datetime import timedelta

# ── Auth constants ─────────────────────────────────────────────────────────────

NONCE_TTL = 300              # seconds a nonce remains valid
CODE_TTL = 30                # seconds a one-time auth code remains valid
RATE_LIMIT_CALLS = 10        # max requests per window per IP
RATE_LIMIT_WINDOW = 60       # seconds

ACCESS_TOKEN_TTL = timedelta(minutes=30)
REFRESH_TOKEN_TTL = timedelta(days=7)

TOKEN_AUDIENCE = "cs-finance"

# ── Auth stores ────────────────────────────────────────────────────────────────

_nonces: dict[str, tuple[float, str]] = {}       # nonce → (issued_at, redirect_origin)
_auth_codes: dict[str, tuple[str, float]] = {}   # code → (steam_id, expires_at)
_refresh_store: dict[str, float] = {}            # jti → expires_at (monotonic)
_rate_store: dict[str, list[float]] = defaultdict(list)

# ── Cache constants ────────────────────────────────────────────────────────────

# steamwebapi.com free plan: 5 req/day — cache for 23 h to stay under the limit
PROFILE_CACHE_TTL = 82800
INVENTORY_CACHE_TTL = 82800
MARKET_INDEX_CACHE_TTL = 82800
ITEM_HISTORY_CACHE_TTL = 82800

# ── Cache stores ───────────────────────────────────────────────────────────────

_profile_cache: dict[str, tuple[dict, float]] = {}
_inventory_cache: dict[str, tuple[list, float]] = {}
_market_index_cache: dict[str, tuple[dict, float]] = {}
_item_history_cache: dict[str, tuple[list, float]] = {}
