"""Caché de steam/ con política explícita (CLEAN-16).

`base_cache.TtlCache` + `CacheState`; `policy.CachePolicy` y una política por tipo de
dato. Las instancias por dominio (history/market/user/image) llegan en la Tarea 3.2.
"""
from steam.cache.base_cache import CacheState, TtlCache
from steam.cache.policy import CachePolicy

__all__ = ["CachePolicy", "CacheState", "TtlCache"]
