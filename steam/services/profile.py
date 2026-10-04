"""Perfil de Steam del usuario (GET /me), con caché de 23 h (CLEAN-11)."""
import time

import httpx

from stores import _profile_cache
from steam.clients import steamwebapi


async def get_profile(client: httpx.AsyncClient, steam_id: str) -> dict:
    now = time.monotonic()
    profile = _profile_cache.fresh(steam_id, now)
    if profile is not None:
        return profile if "steam64_id" in profile else {**profile, "steam64_id": steam_id}

    data = await steamwebapi.profile(client, steam_id)
    if isinstance(data, list):
        data = data[0] if data else {}

    profile = {
        "userName":       data.get("personaname", ""),
        "avatarUrl":      data.get("avatarfull", ""),
        "avatarThumbUrl": data.get("avatarmedium") or data.get("avatarfull", ""),
        "profileUrl":     data.get("profileurl", ""),
        "isOnline":       data.get("personastate", 0) != 0,
        "steam64_id":     steam_id,
    }
    _profile_cache.put(steam_id, profile, now)
    return profile
