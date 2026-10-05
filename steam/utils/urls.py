"""URLs: el CDN de imágenes de Steam y qué es una URL absoluta. Sin dependencias internas."""

STEAM_CDN = "https://community.akamai.steamstatic.com"
_ECONOMY_IMAGE_PATH = "/economy/image/"


def is_http_url(value: str) -> bool:
    """`http://…` o `https://…` (steamwebapi devuelve la imagen ya absoluta en /items)."""
    return value.startswith(("http://", "https://"))


def steam_cdn_url(path_or_hash: str) -> str:
    """Ruta relativa (`/economy/image/<hash>`) o hash pelado → URL absoluta del CDN."""
    if path_or_hash.startswith(_ECONOMY_IMAGE_PATH):
        return STEAM_CDN + path_or_hash
    return f"{STEAM_CDN}{_ECONOMY_IMAGE_PATH}{path_or_hash}"
