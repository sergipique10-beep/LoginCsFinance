"""Mapper de los puntos de histórico del índice de mercado (`/market-index/cs2`)."""
from steam.domain.models import MarketIndexPoint


def _map_market_index_point(point: dict) -> MarketIndexPoint:
    return {
        "date":   str(point.get("ts", "")),
        "price":  float(point.get("value") or 0),
        "change": float(point.get("change") or 0),
        "volume": int(point.get("volume") or 0),
    }
