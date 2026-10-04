"""Constantes del dominio de steam/ (CLEAN-10): desgastes, categorías, mercados y
proveedores. Inmutables: un llamador que mute una constante compartida la cambiaría
para todos los demás.
"""
from collections.abc import Mapping
from types import MappingProxyType

from steam.domain.models import MarketProvider

# Desgastes de CS2, en orden. Respaldo cuando el catálogo de ByMykel no trae los de una skin.
WEAR_NAMES: tuple[str, ...] = ("Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred")


# itemtype crudo (steamwebapi) → categoría de alto nivel para el filtro del frontend.
# La API devuelve itemtype como el arma específica en minúsculas ("ak-47"); el frontend
# agrupa por esta categoría. Cubre el set estable de armas/items de CS2.
WEAPON_CATEGORY: Mapping[str, str] = MappingProxyType({
    # Rifles
    "ak-47": "Rifle", "m4a4": "Rifle", "m4a1-s": "Rifle", "galil ar": "Rifle",
    "famas": "Rifle", "aug": "Rifle", "sg 553": "Rifle",
    # Sniper Rifles
    "awp": "Sniper Rifle", "ssg 08": "Sniper Rifle", "scar-20": "Sniper Rifle", "g3sg1": "Sniper Rifle",
    # Pistols
    "glock-18": "Pistol", "usp-s": "Pistol", "p2000": "Pistol", "p250": "Pistol",
    "five-seven": "Pistol", "tec-9": "Pistol", "cz75-auto": "Pistol", "dual berettas": "Pistol",
    "desert eagle": "Pistol", "r8 revolver": "Pistol",
    # SMGs
    "mac-10": "SMG", "mp9": "SMG", "mp7": "SMG", "mp5-sd": "SMG", "ump-45": "SMG",
    "p90": "SMG", "pp-bizon": "SMG",
    # Heavy
    "nova": "Heavy", "xm1014": "Heavy", "sawed-off": "Heavy", "mag-7": "Heavy",
    "m249": "Heavy", "negev": "Heavy",
    # Knives
    "knife": "Knife", "bayonet": "Knife", "karambit": "Knife", "m9 bayonet": "Knife",
    "butterfly knife": "Knife", "flip knife": "Knife", "gut knife": "Knife",
    "huntsman knife": "Knife", "falchion knife": "Knife", "bowie knife": "Knife",
    "shadow daggers": "Knife", "navaja knife": "Knife", "stiletto knife": "Knife",
    "talon knife": "Knife", "ursus knife": "Knife", "classic knife": "Knife",
    "paracord knife": "Knife", "survival knife": "Knife", "nomad knife": "Knife",
    "skeleton knife": "Knife", "kukri knife": "Knife",
})


def weapon_category(itemtype: str | None) -> str | None:
    """Deriva la categoría de alto nivel ('Rifle', 'Knife'...) desde el itemtype crudo.

    La API de steamwebapi devuelve itemtype como el arma específica en minúsculas
    ('ak-47'). El frontend agrupa por categoría. Para itemtypes no presentes en la
    tabla (gloves variantes, agents, stickers, cases...), devuelve el itemtype
    capitalizado como fallback razonable en vez de None, para que el item siga siendo
    filtrable en su propia categoría en vez de desaparecer del filtro.
    """
    if not itemtype:
        return None
    key = itemtype.strip().lower()
    if key in WEAPON_CATEGORY:
        return WEAPON_CATEGORY[key]
    # Fallbacks por substring para familias no enumerables exhaustivamente.
    if "glove" in key:
        return "Gloves"
    if "knife" in key or "bayonet" in key or "daggers" in key or "karambit" in key:
        return "Knife"
    if "sticker" in key:
        return "Sticker"
    if "agent" in key or "operator" in key:
        return "Agent"
    # Último recurso: itemtype capitalizado (ej. "music kit" → "Music Kit"),
    # para que el item permanezca filtrable y no se pierda.
    return key.title()


# ── Mercados ──────────────────────────────────────────────────────────────────

# Mercados cuyo precio se añade a cada tarjeta (`csfloatPrice`, `buffPrice`): un lookup
# de la lista entera por mercado (services._enrich_market_prices).
TRACKED_MARKETS: tuple[str, ...] = ("csfloat", "buff")

# Mercados que acepta GET /market/prices (passthrough de steamwebapi /market/{m}/prices).
VALID_MARKETS: frozenset[str] = frozenset({
    "buff", "skinport", "skinbaron", "dmarket", "waxpeer",
    "bitskins", "csgotm", "haloskins", "tradeit", "skinbid",
    "csfloat", "youpin",
})

# Markets soportados por el endpoint por-market de steamwebapi (market/<m>/history).
# Steam usa la ruta legacy (steam/api/history) sin market — se deja fuera de aquí.
HISTORY_MARKETS: frozenset[str] = frozenset({"buff", "csfloat"})


# ── Proveedores (GET /market/providers) ───────────────────────────────────────

STEAM_FAVICON = "https://store.steampowered.com/favicon.ico"

# Proveedores que se buscan en /info/markets (Steam va siempre, con su favicon).
PROVIDER_IDS: frozenset[str] = frozenset({"csfloat", "buff"})

# Known public logos used as fallback when the API doesn't return them
KNOWN_LOGOS: Mapping[str, str] = MappingProxyType({
    "steam":   STEAM_FAVICON,
    "csfloat": "https://csfloat.com/favicon.ico",
    "buff":    "https://buff.163.com/favicon.ico",
})

_FALLBACK_PROVIDERS: tuple[Mapping[str, str], ...] = (
    MappingProxyType({"id": "steam",   "name": "Steam",   "logoUrl": KNOWN_LOGOS["steam"]}),
    MappingProxyType({"id": "csfloat", "name": "CSFloat", "logoUrl": KNOWN_LOGOS["csfloat"]}),
    MappingProxyType({"id": "buff",    "name": "Buff163", "logoUrl": KNOWN_LOGOS["buff"]}),
)


def fallback_providers() -> list[MarketProvider]:
    """La lista de respaldo, como copia: antes se devolvía por referencia y un llamador
    que la mutara la cambiaba para todos. Mismo JSON."""
    return [MarketProvider(id=p["id"], name=p["name"], logoUrl=p["logoUrl"]) for p in _FALLBACK_PROVIDERS]


def fallback_provider(provider_id: str) -> MarketProvider:
    return next(p for p in fallback_providers() if p["id"] == provider_id)
