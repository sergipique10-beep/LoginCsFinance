"""Perfiles de Steam (COST-07): ISteamUser/GetPlayerSummaries v2, lotes de hasta 100 SteamID.

La clave (STEAM_WEB_API_KEY) va en la query porque así lo exige la Web API de Valve; el
`SecretFilter` de worker/logs.py la enmascara en cualquier log.
"""
import os
from typing import Any

from worker.sources._fetch import get_json

URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
BATCH = 100


class MissingKey(Exception):
    """STEAM_WEB_API_KEY sin definir."""


async def summaries(state: Any, steam_ids: list[str]) -> list[dict[str, Any]]:
    key = os.environ.get("STEAM_WEB_API_KEY", "")
    if not key:
        raise MissingKey("STEAM_WEB_API_KEY")
    out: list[dict[str, Any]] = []
    for i in range(0, len(steam_ids), BATCH):
        data = await get_json(state, URL, params={"key": key, "steamids": ",".join(steam_ids[i:i + BATCH])})
        out += ((data or {}).get("response") or {}).get("players") or []
    return out


def to_row(p: dict[str, Any]) -> dict[str, Any]:
    """Fila de steam_profiles (docs/sql/steam_profiles.sql)."""
    return {"steam_id": str(p.get("steamid")), "persona_name": p.get("personaname"),
            "avatar_full": p.get("avatarfull"), "avatar_medium": p.get("avatarmedium"),
            "profile_url": p.get("profileurl"), "persona_state": p.get("personastate"),
            "visibility": p.get("communityvisibilitystate")}
