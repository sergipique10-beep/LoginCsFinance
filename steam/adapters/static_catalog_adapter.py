"""Adapter del catálogo de ByMykel/CSGO-API: cada JSON es una lista de entradas."""
from collections.abc import Mapping
from functools import partial
from typing import Any

from steam.adapters._common import mappings, require_list
from steam.domain.models import CatalogEntry
from steam.domain.validators import as_bool, as_str

SOURCE = "bymykel"


def adapt_catalog_source(raw: Any, *, label: str) -> list[CatalogEntry]:
    """`label` es la fuente (skins, knives, stickers…), solo para el mensaje de error.
    `wears` solo viene en skins y knives; en el resto queda vacío."""
    s = partial(as_str, source=SOURCE, op=label)
    entries: list[CatalogEntry] = []
    for e in mappings(require_list(raw, source=SOURCE, op=label), source=SOURCE, op=label):
        rarity_raw, wears_raw = e.get("rarity"), e.get("wears")
        rarity: Mapping = rarity_raw if isinstance(rarity_raw, Mapping) else {}
        wears: list = wears_raw if isinstance(wears_raw, list) else []
        color = s(rarity.get("color"), field="rarity.color")
        entries.append(CatalogEntry(
            name=s(e.get("name"), field="name"),
            market_hash_name=s(e.get("market_hash_name"), field="market_hash_name"),
            image=s(e.get("image"), field="image"),
            rarity_name=s(rarity.get("name"), field="rarity.name"),
            rarity_color=color.lstrip("#") if color else None,
            wears=tuple(w for w in (s(x.get("name"), field="wears.name") for x in wears if isinstance(x, Mapping)) if w),
            stattrak=bool(as_bool(e.get("stattrak"), field="stattrak", source=SOURCE, op=label)),
        ))
    return entries
