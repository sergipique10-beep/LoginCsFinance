"""Cachés del histórico."""
from steam.cache import policy
from steam.cache.base_cache import TtlCache

# Compartida por /item/history (`name:interval:market:days`) y fetch_history_for_item
# (`name:csfloat:35d`); un vacío vale `empty_ttl` (5 min), no 23 h.
_item_history_cache = TtlCache.from_policy(policy.ITEM_HISTORY, name="item_history")
# "latest" → (gainers, losers): el respaldo de los rankings cuando /items falla (CAL-12).
_topmovers_raw_cache = TtlCache.from_policy(policy.TOPMOVERS_RAW, name="topmovers_raw")
