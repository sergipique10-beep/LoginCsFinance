"""Catálogo ByMykel (skins_not_grouped y demás ficheros) → CatalogItem (COST-03)."""
from typing import Any

from worker.adapters._common import name_of
from worker.models import CatalogItem


def _sub(row: dict[str, Any], key: str, field: str = "name") -> Any:
    v = row.get(key)
    return v.get(field) if isinstance(v, dict) else None


def adapt_catalog(raw: Any) -> list[CatalogItem]:
    seen: dict[str, CatalogItem] = {}
    for row in raw if isinstance(raw, list) else []:
        name = name_of(row)
        if name and name not in seen:
            seen[name] = CatalogItem(name, _sub(row, "weapon"), _sub(row, "category"), _sub(row, "rarity"),
                                     _sub(row, "rarity", "color"), row.get("image"))
    return list(seen.values())
