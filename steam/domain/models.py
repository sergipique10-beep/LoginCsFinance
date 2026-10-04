"""Formas de salida de steam/ (CLEAN-08): el contrato JSON con el front escrito como tipo.

`TypedDict` y no `dataclass` porque las rutas devuelven dicts y el contrato son esos
dicts: anotar no cambia el JSON. Las claves son exactamente las que fijan los tests de
contrato de CAL-09 (`tests/test_steam_models.py` lo comprueba).
"""
from dataclasses import dataclass
from typing import Any, Generic, Literal, NotRequired, TypeVar, TypedDict

T = TypeVar("T")
FetchStatus = Literal["ok", "partial", "stale", "error"]


class RankedCard(TypedDict):
    """Tarjeta de skin servida desde un snapshot de ranking (`_row_to_item`).

    Sin `liquidityBreakdown` a propósito: el detail sheet del front usa su ausencia
    como señal de "snapshot pobre, pide el item completo a /market/price".
    """
    id: str
    name: str
    slug: str
    weaponType: str | None
    itemName: str | None
    itemType: str | None
    image: str
    rarity: str
    rarityColor: str
    borderColor: str
    quality: str
    isStatTrak: bool
    isSouvenir: bool
    isStar: bool
    exterior: str | None
    floatValue: float | None
    floatMin: float | None
    floatMax: float | None
    paintIndex: int | None
    phase: str | None
    priceLatest: float
    csfloatPrice: float | None
    buffPrice: float | None
    priceSafe: float
    priceMin: float
    priceMax: float
    priceDelta24h: float | None
    priceDelta7d: float | None
    priceDelta30d: float | None
    priceReal: float | None
    sold24h: int
    sold7d: int
    sold30d: int
    soldTotal: int
    offerVolume: int
    hoursToSold: float
    steamUrl: str | None


class SkinCard(RankedCard):
    """Tarjeta completa (`_map_item`): la de /inventory, /market/items y /market/price."""
    externalPrices: list[dict[str, Any]]
    buyOrderVolume: int
    buyOrderPrice: float
    liquidityScore: float | None
    liquidityBreakdown: dict[str, Any] | None
    marketable: bool
    tradable: bool
    tradeLockDays: Any


class MoverItem(SkinCard):
    """Tarjeta de topmovers (`_map_topmovers_item`). `_change24h` es solo la clave de
    orden de `_build_movers_from_topmovers`, que la borra antes de devolver."""
    _change24h: NotRequired[float]


class RankingRow(TypedDict):
    """Fila de `market_trending` / `market_movers` (`_to_row`). `bucket` solo en movers."""
    name: str
    rank: int
    slug: str
    weapon_type: str | None
    item_name: str | None
    item_type: str | None
    image: str
    rarity: str
    rarity_color: str
    border_color: str
    quality: str
    is_stat_trak: bool
    is_souvenir: bool
    is_star: bool
    exterior: str | None
    float_min: float | None
    float_max: float | None
    paint_index: int | None
    phase: str | None
    price_latest: float
    csfloat_price: float | None
    buff_price: float | None
    price_delta_24h: float | None
    price_delta_7d: float | None
    price_delta_30d: float | None
    price_real: float | None
    sold_24h: int
    sold_7d: int
    sold_30d: int
    offer_volume: int
    hours_to_sold: float | None
    steam_url: str | None
    turnover: float
    bucket: NotRequired[str]


class MarketIndexPoint(TypedDict):
    date: str
    price: float
    change: float
    volume: int


class NewsItem(TypedDict):
    id: str
    category: str
    categoryColor: str
    title: str
    source: str
    date: str
    imageUrl: str
    featured: bool
    url: str
    content: str


class MarketProvider(TypedDict):
    id: str
    name: str
    logoUrl: str


class HistoryPoint(TypedDict):
    """Punto de histórico de precio (/item/history y el enriquecimiento de csfloat)."""
    date: str
    price: float
    volume: int


@dataclass(frozen=True)
class Fetched(Generic[T]):
    """Resultado de un service con camino de degradación (CLEAN-12).

    `status`: `ok` (dato bueno), `stale` (caché caducada), `partial` (respaldo
    incompleto: topmovers, proveedores estáticos) o `error` (vacío o nada que servir).
    `reason` dice por qué. Las rutas devuelven `data` tal cual: exponer el estado al
    front es UX-46.
    """
    data: T
    status: FetchStatus = "ok"
    reason: str | None = None
