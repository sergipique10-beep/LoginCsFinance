"""Catálogo ByMykel (COST-03), semanal: nombres, arma, categoría, rareza e imagen."""
from typing import Any

from worker.sources._fetch import get_json

BASE = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en"
FILES = ("skins_not_grouped", "stickers", "crates", "agents", "keychains", "patches",
         "music_kits", "graffiti", "collectibles")


async def all_files(state: Any) -> list[Any]:
    out: list[Any] = []
    for f in FILES:
        data = await get_json(state, f"{BASE}/{f}.json")
        out.extend(data if isinstance(data, list) else [])
    return out
