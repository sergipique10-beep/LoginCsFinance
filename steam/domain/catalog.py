"""Constantes del dominio de steam/ (CLEAN-10): desgastes, categorías, mercados y
proveedores. Inmutables: un llamador que mute una constante compartida la cambiaría
para todos los demás.
"""
from collections.abc import Mapping
from types import MappingProxyType

from steam.domain.enums import Market, Wear, WeaponCategory
from steam.domain.models import MarketProvider

# Desgastes de CS2, en orden. Respaldo cuando el catálogo de ByMykel no trae los de una skin.
# Derivado de `Wear` (CLEAN-17): los strings salen del enum, y son los mismos de siempre.
WEAR_NAMES: tuple[str, ...] = tuple(w.value for w in Wear)

_R, _SR, _P, _SMG, _H, _K = (
    WeaponCategory.RIFLE, WeaponCategory.SNIPER_RIFLE, WeaponCategory.PISTOL,
    WeaponCategory.SMG, WeaponCategory.HEAVY, WeaponCategory.KNIFE,
)


# itemtype crudo (steamwebapi) → categoría de alto nivel para el filtro del frontend.
# La API devuelve itemtype como el arma específica en minúsculas ("ak-47"); el frontend
# agrupa por esta categoría. Cubre el set estable de armas/items de CS2.
WEAPON_CATEGORY: Mapping[str, WeaponCategory] = MappingProxyType({
    # Rifles
    "ak-47": _R, "m4a4": _R, "m4a1-s": _R, "galil ar": _R,
    "famas": _R, "aug": _R, "sg 553": _R,
    # Sniper Rifles
    "awp": _SR, "ssg 08": _SR, "scar-20": _SR, "g3sg1": _SR,
    # Pistols
    "glock-18": _P, "usp-s": _P, "p2000": _P, "p250": _P,
    "five-seven": _P, "tec-9": _P, "cz75-auto": _P, "dual berettas": _P,
    "desert eagle": _P, "r8 revolver": _P,
    # SMGs
    "mac-10": _SMG, "mp9": _SMG, "mp7": _SMG, "mp5-sd": _SMG, "ump-45": _SMG,
    "p90": _SMG, "pp-bizon": _SMG,
    # Heavy
    "nova": _H, "xm1014": _H, "sawed-off": _H, "mag-7": _H,
    "m249": _H, "negev": _H,
    # Knives
    "knife": _K, "bayonet": _K, "karambit": _K, "m9 bayonet": _K,
    "butterfly knife": _K, "flip knife": _K, "gut knife": _K,
    "huntsman knife": _K, "falchion knife": _K, "bowie knife": _K,
    "shadow daggers": _K, "navaja knife": _K, "stiletto knife": _K,
    "talon knife": _K, "ursus knife": _K, "classic knife": _K,
    "paracord knife": _K, "survival knife": _K, "nomad knife": _K,
    "skeleton knife": _K, "kukri knife": _K,
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
        return WEAPON_CATEGORY[key].value
    # Fallbacks por substring para familias no enumerables exhaustivamente.
    if "glove" in key:
        return WeaponCategory.GLOVES.value
    if "knife" in key or "bayonet" in key or "daggers" in key or "karambit" in key:
        return WeaponCategory.KNIFE.value
    if "sticker" in key:
        return WeaponCategory.STICKER.value
    if "agent" in key or "operator" in key:
        return WeaponCategory.AGENT.value
    # Último recurso: itemtype capitalizado (ej. "music kit" → "Music Kit"),
    # para que el item permanezca filtrable y no se pierda.
    return key.title()


# ── Mercados ──────────────────────────────────────────────────────────────────

# Los tres se derivan de `Market` (CLEAN-17), como `.value`: fuera de domain/ se
# construyen URLs y claves de caché con ellos y un miembro del enum no formatea igual.

# Mercados cuyo precio se añade a cada tarjeta (`csfloatPrice`, `buffPrice`): un lookup
# de la lista entera por mercado (services/pricing.enrich_market_prices).
TRACKED_MARKETS: tuple[str, ...] = (Market.CSFLOAT.value, Market.BUFF.value)

# Mercados que steamwebapi sirve en /market/{m}/prices pero que no tienen cliente propio
# en steam/api/ (ni, por tanto, miembro en `Market`): solo pasan por GET /market/prices.
PASSTHROUGH_MARKETS: frozenset[str] = frozenset({
    "skinport", "skinbaron", "dmarket", "waxpeer", "bitskins",
    "csgotm", "haloskins", "tradeit", "skinbid", "youpin",
})

# Markets soportados por el endpoint por-market de steamwebapi (market/<m>/history).
# Steam usa la ruta legacy (steam/api/history) sin market — se deja fuera de aquí.
HISTORY_MARKETS: frozenset[str] = frozenset({Market.BUFF.value, Market.CSFLOAT.value})

# Mercados que acepta GET /market/prices (passthrough de steamwebapi /market/{m}/prices).
VALID_MARKETS: frozenset[str] = HISTORY_MARKETS | PASSTHROUGH_MARKETS


# ── Proveedores (GET /market/providers) ───────────────────────────────────────

STEAM_FAVICON = "https://store.steampowered.com/favicon.ico"

# Proveedores que se buscan en /info/markets (Steam va siempre, con su favicon).
PROVIDER_IDS: frozenset[str] = frozenset({Market.CSFLOAT.value, Market.BUFF.value})

# Known public logos used as fallback when the API doesn't return them
KNOWN_LOGOS: Mapping[str, str] = MappingProxyType({
    Market.STEAM.value:   STEAM_FAVICON,
    Market.CSFLOAT.value: "https://csfloat.com/favicon.ico",
    Market.BUFF.value:    "https://buff.163.com/favicon.ico",
})

_FALLBACK_PROVIDERS: tuple[Mapping[str, str], ...] = (
    MappingProxyType({"id": Market.STEAM.value,   "name": "Steam",   "logoUrl": KNOWN_LOGOS["steam"]}),
    MappingProxyType({"id": Market.CSFLOAT.value, "name": "CSFloat", "logoUrl": KNOWN_LOGOS["csfloat"]}),
    MappingProxyType({"id": Market.BUFF.value,    "name": "Buff163", "logoUrl": KNOWN_LOGOS["buff"]}),
)


def fallback_providers() -> list[MarketProvider]:
    """La lista de respaldo, como copia: antes se devolvía por referencia y un llamador
    que la mutara la cambiaba para todos. Mismo JSON."""
    return [MarketProvider(id=p["id"], name=p["name"], logoUrl=p["logoUrl"]) for p in _FALLBACK_PROVIDERS]


def fallback_provider(provider_id: str) -> MarketProvider:
    return next(p for p in fallback_providers() if p["id"] == provider_id)
