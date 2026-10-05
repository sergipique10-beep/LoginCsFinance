"""Políticas de caché de steam/ (CLEAN-16): UNA constante por tipo de dato, con su TTL y
su motivo. Son la fuente de los valores; `stores.py` solo los reexporta por compatibilidad
hasta la Fase 6. Migrar a Redis (CAL-04) es cambiar `TtlCache`, no esto.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CachePolicy:
    """`ttl` en segundos; `empty_ttl` acorta el TTL de un valor vacío (un histórico `[]`
    no vale 23 h); `fail_ttl` es el backoff tras un fallo (caché negativo, PERF-17);
    `max_entries` acota las cachés con clave del usuario (Render free: 512 MB)."""
    ttl: float
    empty_ttl: float | None = None
    fail_ttl: float = 0
    max_entries: int | None = None


# steamwebapi Starter: 20 req/60 s por endpoint, 2k/día → 23 h para no agotar el día.
_DAY = 82800

# Backoff tras un fallo o un vacío: 5 min en vez del TTL largo, para no reintentar en
# cada petición contra una fuente caída (CAL-08, PERF-17) ni guardar 23 h un vacío.
_SHORT = 300

PROFILE = CachePolicy(_DAY)                       # steam_id → perfil
INVENTORY = CachePolicy(_DAY)                     # steam_id → items
MARKET_INDEX = CachePolicy(_DAY)                  # tf → índice; stale ante 402
# Compartida por /item/history y fetch_history_for_item; el enriquecimiento usa empty_ttl.
ITEM_HISTORY = CachePolicy(_DAY, empty_ttl=_SHORT)
# CAL-12: el respaldo de topmovers caduca con el índice.
TOPMOVERS_RAW = CachePolicy(MARKET_INDEX.ttl)
# Las tres con clave del usuario llevan tope de entradas.
SEARCH = CachePolicy(300, max_entries=200)        # query → ~30 items (~60 KB)
ITEM_PRICE = CachePolicy(300, max_entries=500)    # markethashname.lower() → item con liquidez
# Sin `name`, el valor es la lista entera de precios de un mercado (MB): tope bajo.
MARKET_PRICES = CachePolicy(300, max_entries=100)
MARKET_LOOKUP = CachePolicy(_DAY, fail_ttl=_SHORT)     # market → {name: price} (PERF-17)
MARKET_PROVIDERS = CachePolicy(_DAY, fail_ttl=_SHORT)  # "providers" → lista, casi estática
IMAGE_CATALOG = CachePolicy(_DAY, fail_ttl=_SHORT)     # catálogo ByMykel; fallo total → backoff (CAL-08)
NEWS = CachePolicy(1800)      # /news/cs2: Steam + 5 og:image por petición (PERF-06)
FX = CachePolicy(86400)       # el BCE publica un tipo al día (UX-08)
LEETIFY = CachePolicy(300)    # misma frescura que el staleTime del front (SEC-09)
INVENTORY_REFRESH_COOLDOWN = CachePolicy(3600)   # botón «forzar refresh»: 1 h por usuario
