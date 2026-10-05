"""Perfil de Steam del usuario (GET /me), con caché de 23 h (CLEAN-11)."""
import time

import httpx

from steam.cache.user_cache import _profile_cache
from steam.adapters.steam_adapter import adapt_profile
from steam.api import steam_client
from steam.domain.models import Fetched
from steam.errors.handling import degraded
from steam.mappers.profile_mapper import _map_profile


async def get_profile(client: httpx.AsyncClient, steam_id: str) -> Fetched[dict]:
    """`ok` con el perfil; `error` (`reason="empty_body"`) con los campos en blanco si
    steamwebapi respondió 200 sin perfil. Los errores de la fuente suben tipados."""
    now = time.monotonic()
    profile = _profile_cache.fresh(steam_id, now)
    if profile is not None:
        return Fetched(profile if "steam64_id" in profile else {**profile, "steam64_id": steam_id})

    data = adapt_profile(await steam_client.profile(client, steam_id))   # forma rara → UnexpectedPayload
    profile = _map_profile(data, steam_id)
    if data is None:
        # 200 sin perfil: campos vacíos SIN cachear (CAL-14: antes se guardaban 23 h).
        return degraded("profile", "empty_body", "empty", profile)
    _profile_cache.put(steam_id, profile, now)
    return Fetched(profile)
