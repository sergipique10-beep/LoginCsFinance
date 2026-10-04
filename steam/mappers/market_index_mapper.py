"""Mapper de los puntos de histórico del índice de mercado (`/market-index/cs2`)."""
from steam.domain.models import IndexPoint, MarketIndexPoint


def _map_market_index_point(point: IndexPoint) -> MarketIndexPoint:
    return {
        "date":   point.ts,
        "price":  point.value if point.value is not None else 0.0,
        "change": point.change if point.change is not None else 0.0,
        "volume": point.volume if point.volume is not None else 0,
    }
