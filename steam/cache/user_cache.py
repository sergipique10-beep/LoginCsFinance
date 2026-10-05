"""Cachés por usuario y de noticias."""
from steam.cache import policy
from steam.cache.base_cache import TtlCache

_profile_cache = TtlCache.from_policy(policy.PROFILE, name="profile")        # steam_id → perfil
_inventory_cache = TtlCache.from_policy(policy.INVENTORY, name="inventory")  # steam_id → items
# steam_id → marca del último «forzar refresh»; `fresh` = cooldown activo (antes un dict
# con `monotonic` a mano en routes/items.py).
_inventory_refresh_cooldown = TtlCache.from_policy(policy.INVENTORY_REFRESH_COOLDOWN,
                                                   name="inventory_refresh_cooldown")
_news_cache = TtlCache.from_policy(policy.NEWS, name="news")   # count → items
