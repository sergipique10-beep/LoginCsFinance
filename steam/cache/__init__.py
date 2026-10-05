"""Caché de steam/ con política explícita.

`base_cache.TtlCache` + `CacheState`; `policy.CachePolicy` y una política por tipo de
dato; instancias por dominio en history_cache / market_cache / user_cache / image_cache.
`ALL_CACHES` es el registro: `clear_all()` (tests) y `stats_all()` (línea `[steam-cache]`
del cap-tick). Es la única casa de estas cachés: nada se reexporta desde stores.py.
"""
from steam.cache import history_cache, image_cache, market_cache, user_cache
from steam.cache.base_cache import CacheState, TtlCache
from steam.cache.image_cache import CatalogCache
from steam.cache.policy import CachePolicy

ALL_CACHES: dict[str, TtlCache | CatalogCache] = {
    **{c.name: c for m in (history_cache, market_cache, user_cache)
       for c in vars(m).values() if isinstance(c, TtlCache)},
    "image_catalog": image_cache.catalog_cache,
}


def clear_all() -> None:
    for cache in ALL_CACHES.values():
        cache.clear()


def stats_all() -> dict[str, dict[str, int]]:
    return {name: cache.stats() for name, cache in ALL_CACHES.items()}


__all__ = ["ALL_CACHES", "CachePolicy", "CacheState", "CatalogCache", "TtlCache", "clear_all", "stats_all"]
