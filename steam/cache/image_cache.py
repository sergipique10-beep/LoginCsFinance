"""Catálogo estático de ByMykel en memoria (CLEAN-16): imágenes, rareza y la marca de
carga buena con su backoff (CAL-08), que antes eran tres dicts sueltos en stores.py.
"""
from collections.abc import Iterable

from steam.cache import policy
from steam.cache.base_cache import TtlCache

_META_KEY = "catalog"   # → nº de entradas de la última carga buena


class CatalogCache:
    """`images` y `rarities` siguen siendo dicts (los tests los leen así); `meta` es la
    `TtlCache` que dice si la última carga vale (`fresh`) o si hubo un fallo total
    (`in_backoff`, 5 min en vez de reintentar en cada petición)."""

    def __init__(self) -> None:
        self.images: dict[str, str] = {}                     # markethashname/marketname → URL
        self.rarities: dict[str, tuple[str, str]] = {}       # markethashname → (rareza, color hex)
        self.meta = TtlCache.from_policy(policy.IMAGE_CATALOG, name="image_catalog")

    def register(self, keys: Iterable[str], image: str, rarity: tuple[str, str] | None = None) -> None:
        for key in keys:
            self.images[key] = image
            if rarity:
                self.rarities[key] = rarity

    def image_for(self, candidates: Iterable[str]) -> str:
        """La primera imagen conocida entre los nombres candidatos, o ""."""
        return next((img for key in candidates if (img := self.images.get(key))), "")

    def rarity_for(self, name: str) -> tuple[str, str] | None:
        return self.rarities.get(name)

    def mark_loaded(self, now: float | None = None) -> None:
        self.meta.put(_META_KEY, len(self.images), now)

    def mark_failed(self, now: float | None = None) -> None:
        self.meta.mark_failed(_META_KEY, now)

    def is_fresh_or_backoff(self, now: float | None = None) -> bool:
        """True si no toca recargar: la última carga vale o estamos en backoff tras un fallo."""
        return self.meta.fresh(_META_KEY, now) is not None or self.meta.in_backoff(_META_KEY, now)

    def __len__(self) -> int:
        return len(self.images)

    def __bool__(self) -> bool:
        return bool(self.images)

    def clear(self) -> None:
        self.images.clear()
        self.rarities.clear()
        self.meta.clear()

    def stats(self) -> dict[str, int]:
        # hits/misses/stale_served son los de `meta` (la marca de carga); entries, del catálogo.
        return {**self.meta.stats(), "entries": len(self.images), "rarities": len(self.rarities)}


catalog_cache = CatalogCache()
