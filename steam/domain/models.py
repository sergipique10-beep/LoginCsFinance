"""Modelos de steam/: los TypedDict de salida (CLEAN-08) y los modelos internos (CLEAN-14).

Salida: el contrato JSON con el front escrito como tipo.

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


# ── Modelos internos (CLEAN-14) ────────────────────────────────────────────────
# Lo que los adapters (`steam/adapters/`) construyen a partir del JSON crudo de cada
# fuente, ya con tipos: `None` significa «el campo no vino o no era convertible», y
# `0` significa cero. Son dataclasses inmutables y NO son contrato con el front: los
# mappers los convierten a los TypedDict de arriba.


@dataclass(frozen=True)
class PriceQuote:
    """Un precio por mercado dentro de `prices[]` de steamwebapi."""
    market: str | None
    price: float | None
    quantity: int | None


@dataclass(frozen=True)
class Variant:
    """Una variante de `variants[]` (fases de Doppler)."""
    paint_index: int | None
    phase: str | None


@dataclass(frozen=True)
class SteamItem:
    """Un item de steamwebapi (`/items`, `/inventory`, `/item`, gainers de topmovers).

    `name` es `markethashname` (canónico, en inglés) o, si falta, `marketname`.
    `latest_price` es la cadena que ya usaban `_map_item` y el Liquidity Score:
    pricelatestsell → price → lowestprice → priceusd (el primero > 0), o None.
    """
    id: str | None
    asset_id: str | None
    market_hash_name: str | None
    market_name: str | None
    slug: str | None
    weapon_type: str | None
    item_type: str | None
    item_name: str | None
    image: str | None
    rarity: str | None
    color: str | None
    border_color: str | None
    quality: str | None
    is_stattrak: bool | None
    is_souvenir: bool | None
    is_star: bool | None
    exterior: str | None
    float_value: float | None
    float_min: float | None
    float_max: float | None
    paint_index: int | None
    variants: tuple[Variant, ...]
    price_latest_sell: float | None
    price: float | None
    lowest_price: float | None
    price_usd: float | None
    price_latest: float | None
    price_median: float | None
    price_real: float | None
    price_real_24h: float | None
    price_real_7d: float | None
    price_real_30d: float | None
    price_safe: float | None
    price_min: float | None
    price_max: float | None
    prices: tuple[PriceQuote, ...]
    sold_24h: int | None
    sold_7d: int | None
    sold_30d: int | None
    sold_total: int | None
    offer_volume: int | None
    buy_order_volume: int | None
    buy_order_price: float | None
    hours_to_sold: float | None
    marketable: bool | None
    tradable: bool | None
    trade_lock_days: Any
    steam_url: str | None

    @property
    def name(self) -> str:
        return self.market_hash_name or self.market_name or ""

    @property
    def latest_price(self) -> float | None:
        for value in (self.price_latest_sell, self.price, self.lowest_price, self.price_usd):
            if value:
                return value
        return None


@dataclass(frozen=True)
class TopMover:
    """Un gainer/loser de `topmovers` en `/market-index/cs2`: el item y su variación
    24 h en PORCENTAJE (UX-35)."""
    item: SteamItem
    change_24h: float | None


@dataclass(frozen=True)
class IndexPoint:
    ts: str
    value: float | None
    change: float | None
    volume: int | None


@dataclass(frozen=True)
class MarketIndexData:
    """`/market-index/cs2`: la serie, los topmovers y los agregados del día."""
    history: tuple[IndexPoint, ...]
    gainers: tuple[TopMover, ...]
    losers: tuple[TopMover, ...]
    turnover_24h: float | None
    sold_24h: int | None
    price_index: float | None
    real_price_index: float | None
    buy_order_price_index: float | None
    dropped_movers: int = 0   # topmovers sin `markethashname`, descartados por el adapter


@dataclass(frozen=True)
class ProfileData:
    persona_name: str | None
    avatar_full: str | None
    avatar_medium: str | None
    profile_url: str | None
    persona_state: int | None


@dataclass(frozen=True)
class NewsEntry:
    gid: str | None
    title: str | None
    url: str | None
    contents: str | None
    date: int | None
    author: str | None
    feed_label: str | None
    feed_name: str | None


@dataclass(frozen=True)
class CatalogEntry:
    """Una entrada del catálogo de ByMykel (skins/knives con `wears`, el resto plano)."""
    name: str | None
    market_hash_name: str | None
    image: str | None
    rarity_name: str | None
    rarity_color: str | None    # hex sin '#'
    wears: tuple[str, ...]
    stattrak: bool


@dataclass(frozen=True)
class ProviderInfo:
    """Un mercado de `/info/markets`: `id` en minúsculas (id → key → name)."""
    id: str
    name: str | None
    logo: str | None


@dataclass(frozen=True)
class PriceRow:
    """Una fila de `/market/{market}/prices`: nombre y precio > 0."""
    name: str
    price: float


@dataclass(frozen=True)
class FxRates:
    eur: float | None
