"""Precio de referencia + catálogo + ventas → tarjeta `SkinCard` (COST-05). Puro.

Es el `own_adapter` del diseño § 2.5: la misma forma que `_map_item` produce desde steamwebapi,
para que `rules.ranking_eligible / turnover / diversificar / is_sticker_slab` y `row_mapper._to_row`
trabajen sin cambios y las filas de las tablas sombra sean idénticas a las de hoy. Lo que ninguna
fuente propia trae queda en su valor por defecto (`csfloatPrice`, `floatMin`, `hoursToSold`…).

Deltas: desde la serie propia (`market_ref_daily`: cierre de hace 1, 7 y 30 días) cuando existe;
mientras la serie es joven, 7 d y 30 d se aproximan con las medianas de ventas de Skinport
(`(mediana_24h − mediana_7d) / mediana_7d`), que es el `delta_source='skinport_proxy'` del diseño.
"""
from dataclasses import dataclass, fields
from typing import Any
from urllib.parse import quote

from steam.domain.catalog import WEAR_NAMES, weapon_category
from steam.domain.models import SteamItem
from steam.domain.rules import plausible_ratio
from worker.models import CatalogItem
from worker.pricing.index import SalesStats
from worker.pricing.reference import Reference

STEAM_LISTING = "https://steamcommunity.com/market/listings/730/{name}"
_PREFIXES = ("★ ", "StatTrak™ ", "Souvenir ")


@dataclass(frozen=True)
class Closes:
    """Cierres de la serie propia para los deltas (None = la serie no llega tan atrás)."""
    d1: float | None = None
    d7: float | None = None
    d30: float | None = None


def pct(now: float | None, old: float | None) -> float | None:
    if not now or not old or not plausible_ratio(now, old):
        return None
    return round((now - old) / old * 100, 2)


def deltas(ref: float | None, closes: Closes, s: SalesStats | None) -> tuple[float | None, float | None, float | None]:
    proxy = s.median_24h if s else None
    d7 = pct(ref, closes.d7) if closes.d7 else pct(proxy, s.median_7d if s else None)
    d30 = pct(ref, closes.d30) if closes.d30 else pct(proxy, s.median_30d if s else None)
    return pct(ref, closes.d1), d7, d30


def split_name(name: str) -> tuple[str | None, str | None]:
    """'StatTrak™ AK-47 | Redline (Field-Tested)' → ('Redline', 'Field-Tested')."""
    base = name
    for p in _PREFIXES:
        base = base.removeprefix(p)
    exterior = None
    if base.endswith(")") and " (" in base:
        base, wear = base.rsplit(" (", 1)
        wear = wear[:-1]
        exterior = wear if wear in WEAR_NAMES else None
        if exterior is None:
            base = f"{base} ({wear})"
    item_name = base.split(" | ", 1)[1] if " | " in base else None
    return item_name, exterior


def _color(c: str | None) -> str:
    return (c or "").lstrip("#") or "b0c3d9"


def card(ref: Reference, cat: CatalogItem | None, s: SalesStats | None, closes: Closes) -> dict[str, Any]:
    name = ref.market_hash_name
    item_name, exterior = split_name(name)
    by_source = {p["market"]: p for p in ref.prices}
    weapon = cat.weapon if cat else None
    d24, d7, d30 = deltas(ref.ref_price_usd, closes, s)
    return {
        "id": name, "name": name, "slug": "",
        "weaponType": weapon_category(weapon) if weapon else (cat.category if cat else None),
        "itemName": item_name, "itemType": (weapon or (cat.category if cat else None) or "").lower() or None,
        "image": (cat.image_url if cat else None) or "",
        "rarity": (cat.rarity if cat else None) or "Base Grade",
        "rarityColor": _color(cat.rarity_color if cat else None), "borderColor": _color(cat.rarity_color if cat else None),
        "quality": "StatTrak™" if name.startswith("StatTrak™") else "Souvenir" if name.startswith("Souvenir") else "Normal",
        "isStatTrak": name.startswith("StatTrak™"), "isSouvenir": name.startswith("Souvenir"),
        "isStar": name.startswith("★"), "exterior": exterior,
        "floatValue": None, "floatMin": None, "floatMax": None, "paintIndex": None, "phase": None,
        "priceLatest": ref.ref_price_usd or 0, "csfloatPrice": None,
        "buffPrice": (by_source.get("buff") or {}).get("price"),
        "priceSafe": 0, "priceMin": 0, "priceMax": 0,
        "priceDelta24h": d24, "priceDelta7d": d7, "priceDelta30d": d30,
        "priceReal": s.median_24h if s else None,
        "sold24h": ref.volume_24h or 0, "sold7d": s.volume_7d if s else 0, "sold30d": s.volume_30d if s else 0,
        "soldTotal": 0, "offerVolume": (by_source.get("skinport") or {}).get("listings") or 0,
        "hoursToSold": 0, "steamUrl": STEAM_LISTING.format(name=quote(name)),
    }


def steam_item(c: dict[str, Any]) -> SteamItem:
    """Lo mínimo de `SteamItem` que miran `ranking_eligible` e `is_sticker_slab`."""
    values: dict[str, Any] = {f.name: None for f in fields(SteamItem)}
    values.update(id=c["name"], market_hash_name=c["name"], market_name=c["name"], item_type=c["itemType"],
                  price_latest_sell=c["priceLatest"] or None, sold_24h=c["sold24h"], variants=(), prices=())
    return SteamItem(**values)
