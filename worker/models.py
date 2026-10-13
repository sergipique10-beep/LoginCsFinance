"""Modelos internos del worker (COST-03). Lo que sale de un adapter y entra en Timescale."""
import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class Quote:
    """Una cotización de una fuente para un ítem en un instante. None = la fuente no lo trae."""
    ts: dt.datetime
    market_hash_name: str
    source: str               # skinport | skinport_sales | marketcsgo | buff
    price_usd: float | None
    volume: int | None = None     # ventas en la ventana que declare la fuente (24 h si se puede)
    listings: int | None = None   # ofertas abiertas

    @property
    def qty(self) -> int:
        """La cantidad que pondera: ventas si las hay; si no, ofertas; mínimo 1."""
        return max(self.volume or self.listings or 0, 1)


@dataclass(frozen=True)
class CatalogItem:
    market_hash_name: str
    weapon: str | None
    category: str | None
    rarity: str | None
    rarity_color: str | None
    image_url: str | None
