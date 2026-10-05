"""Normalizadores de nombres e imágenes de ítem de CS2 (CLEAN-10, CLEAN-17; ex names.py).
Funciones puras: cada prefijo («StatTrak™ », «★ », «Souvenir ») y la marca de slab
viven solo aquí (guardia en tests/test_domain_normalizers.py). Las REGLAS que deciden
algo sobre un ítem (¿es un slab?, ¿entra en el ranking?) están en `domain/rules.py`.

Invariante (CLAUDE.md): el `name` de una tarjeta sale de `markethashname` antes que de
`marketname`, porque el marketname puede venir localizado. Estas reglas reciben ese name.
"""
from collections.abc import Sequence

from steam.domain.catalog import WEAR_NAMES
from steam.utils.urls import is_http_url, steam_cdn_url

STATTRAK = "StatTrak™ "
STAR = "★ "
SOUVENIR = "Souvenir "
# Marca de los sticker slabs, en minúsculas: aparece como `itemtype` («Sticker Slab») y
# como prefijo del nombre («Sticker Slab | Crown (Foil)»). La decisión está en rules.
SLAB_MARK = "sticker slab"


def image_lookup_candidates(name: str, item_type: str | None) -> list[str]:
    """Claves a probar en el catálogo de imágenes, en orden; gana la primera que exista.

    - Las variantes StatTrak™ comparten imagen con la base. Quitar «StatTrak™ » cubre
      los dos casos de una vez: «★ StatTrak™ X (wear)» → «★ X (wear)» (cuchillo, imagen
      de la API) y «StatTrak™ X (wear)» → «X (wear)» (arma, de la API o de ByMykel).
    - «★ X» sin StatTrak™: ByMykel guarda los cuchillos sin la estrella. Se quita del
      nombre original, no del que ya perdió el StatTrak™.
    - Souvenir: la skin base, salvo los charms souvenir, que no son una skin.
    """
    candidates = [name]
    if STATTRAK in name:
        candidates.append(name.replace(STATTRAK, "", 1))
    if name.startswith(STAR):
        candidates.append(name[len(STAR):])
    base = without_souvenir(name)
    if base is not None and "charm" not in (item_type or "").lower():
        candidates.append(base)
    return candidates


def catalog_keys_for_skin(name: str, wears: Sequence[str], stattrak: bool) -> list[str]:
    """Claves con las que se registra una skin del catálogo de ByMykel: base y «★ base»
    (más las StatTrak™ si la skin lo admite), solas y con cada desgaste."""
    wears = wears or WEAR_NAMES
    bases = [name, f"{STAR}{name}"]
    if stattrak:
        bases += [f"{STATTRAK}{name}", f"{STAR}{STATTRAK}{name}"]
    return bases + [f"{base} ({wear})" for base in bases for wear in wears]


def without_souvenir(name: str) -> str | None:
    """El nombre sin «Souvenir », o None si no lo lleva."""
    return name[len(SOUVENIR):] if name.startswith(SOUVENIR) else None


def has_slab_mark(text: str | None) -> bool:
    """¿Lleva la marca de slab (en un `itemtype` o en un nombre)? Sin decidir nada más."""
    return SLAB_MARK in (text or "").lower()


def skin_base(name: str) -> str:
    """Nombre sin el desgaste: 'AK-47 | Crane Flight (Field-Tested)' → sin '(...)'."""
    return name.split(" (")[0].strip().lower()


def name_key(name: str) -> str:
    """Forma comparable de un nombre: la que usan las claves de caché y el match exacto
    de /market/price (steamwebapi /items?search es fuzzy; el front manda el
    markethashname canónico, en inglés, y se compara sin distinguir mayúsculas)."""
    return name.lower()


def names_match(a: str, b: str) -> bool:
    return name_key(a) == name_key(b)


def normalize_image_url(raw: str | None) -> str:
    """Valor `image` de steamwebapi → URL absoluta del CDN de Steam, o "".

    /items e /inventory devuelven la URL completa (community.akamai.steamstatic.com):
    pasa tal cual. Las otras ramas son defensivas (ruta relativa, hash pelado) para
    endpoints menos documentados, como los topmovers de /market-index. La cadena
    vacía se devuelve vacía para que el `@if(imageUrl())` del front no pinte una
    imagen rota.
    """
    if not raw:
        return ""
    if is_http_url(raw):
        return raw
    return steam_cdn_url(raw)
