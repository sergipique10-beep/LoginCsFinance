"""
In-memory stores de auth y la caché de Leetify.

WARNING: these stores are only valid for single-worker deployments.
In multi-worker or multi-instance environments, replace with Redis (TTL-native).
TODO: replace _nonces, _auth_codes, _rate_store and _leetify_cache with Redis.

Las cachés de `steam/` NO viven aquí: están en `steam/cache/` (una instancia por
dominio y los TTL en `steam/cache/policy.py`). Nada de steam/ importa de este módulo.
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

# ── Leetify (stats/router.py, SEC-09) ──────────────────────────────────────────

LEETIFY_CACHE_TTL = policy.LEETIFY.ttl       # 5 min — misma frescura que tenía el staleTime del front
_leetify_cache: dict[tuple[str, str], tuple[Any, float]] = {}  # (steam_id, ruta) → (json, ts)
