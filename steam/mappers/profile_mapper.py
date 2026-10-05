"""Mapper del perfil de Steam (`/profile`) al JSON de GET /me."""
from steam.domain.models import ProfileData


def _map_profile(profile: ProfileData | None, steam_id: str) -> dict:
    """`None` (200 sin perfil) da los campos en blanco, como siempre; cachearlo o no lo
    decide el service."""
    p = profile
    return {
        "userName":       (p.persona_name if p else None) or "",
        "avatarUrl":      (p.avatar_full if p else None) or "",
        "avatarThumbUrl": (p.avatar_medium or p.avatar_full if p else None) or "",
        "profileUrl":     (p.profile_url if p else None) or "",
        "isOnline":       bool(p and p.persona_state),
        "steam64_id":     steam_id,
    }
