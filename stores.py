"""
In-memory stores and TTL constants.

WARNING: these stores are only valid for single-worker deployments.
In multi-worker or multi-instance environments, replace with Redis (TTL-native).
TODO: replace _nonces, _auth_codes, _refresh_store, _rate_store,
      _profile_cache, _inventory_cache, _market_index_cache and
      _item_history_cache with Redis.
"""
import json
import logging
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

_stores_logger = logging.getLogger("uvicorn.error")

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
MOVERS_CACHE_TTL = 82800     # 23 h — free plan: 5 req/day, must match other caches
TRENDING_CACHE_TTL = 82800   # 23 h — same daily budget
SEARCH_CACHE_TTL = 300       # 5 min — search queries cached briefly to avoid hammering the API
MARKET_PRICES_CACHE_TTL = 300  # 5 min — live market prices, updated frequently by steamwebapi
IMAGE_CACHE_TTL = 82800      # 23 h — same budget as other free-plan caches; CDN URLs are stable
MARKET_LOOKUP_CACHE_TTL = 82800  # 23 h — full price list per market (premium endpoint, same daily budget)
MARKET_PROVIDERS_CACHE_TTL = 82800  # 23 h — market list is mostly static

# ── Cache stores ───────────────────────────────────────────────────────────────

_profile_cache: dict[str, tuple[dict, float]] = {}
_inventory_cache: dict[str, tuple[list, float]] = {}
_market_index_cache: dict[str, tuple[dict, float]] = {}
_item_history_cache: dict[str, tuple[list, float]] = {}
_movers_cache: dict[str, tuple[dict, float]] = {}
_topmovers_raw_cache: dict[str, tuple[list, list, float]] = {}  # "latest" → (gainers, losers, ts)
_trending_cache: dict[str, tuple[list, float]] = {}
_search_cache: dict[str, tuple[list, float]] = {}
_market_prices_cache: dict[str, tuple[any, float]] = {}
_item_image_cache: dict[str, str] = {}  # markethashname/marketname → image URL
_image_cache_meta: dict[str, float] = {}  # "ts" → monotonic timestamp of last successful population
_market_lookup_cache: dict[str, tuple[dict, float]] = {}  # market → ({name: price}, ts)
_market_providers_cache: dict[str, tuple[list, float]] = {}  # "providers" → (list, ts)

# ── Market cap history ─────────────────────────────────────────────────────────
# Hourly snapshots of the CS2 market priceindex.
# Shape: [{"ts": "2024-01-01T00:00:00Z", "v": 123.45}, ...]
# Persisted to a JSON file so history survives restarts.

_CAP_HISTORY_MAX = 3 * 365 * 24  # ~3 years of hourly snapshots
_market_cap_history: list[dict] = []


def load_cap_history(path: Path) -> None:
    global _market_cap_history
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                _market_cap_history.clear()
                _market_cap_history.extend(data[-_CAP_HISTORY_MAX:])
                _stores_logger.info("[cap-history] loaded %d snapshots from %s", len(_market_cap_history), path)
    except Exception as exc:
        _stores_logger.warning("[cap-history] could not load %s: %s", path, exc)


def save_cap_history(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_market_cap_history, f, separators=(",", ":"))
    except Exception as exc:
        _stores_logger.warning("[cap-history] could not save %s: %s", path, exc)
