"""Cachés del mercado."""
from steam.cache import policy
from steam.cache.base_cache import TtlCache

_market_index_cache = TtlCache.from_policy(policy.MARKET_INDEX, name="market_index")   # tf → índice
_search_cache = TtlCache.from_policy(policy.SEARCH, name="search")               # "<ns>:<query>" → items
_item_price_cache = TtlCache.from_policy(policy.ITEM_PRICE, name="item_price")   # name.lower() → item
_market_prices_cache = TtlCache.from_policy(policy.MARKET_PRICES, name="market_prices")
_market_lookup_cache = TtlCache.from_policy(policy.MARKET_LOOKUP, name="market_lookup")   # market → {name: price}
_market_providers_cache = TtlCache.from_policy(policy.MARKET_PROVIDERS, name="market_providers")
_fx_cache = TtlCache.from_policy(policy.FX, name="fx")   # "usdeur" → tasa; stale si cae frankfurter
