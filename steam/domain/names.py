"""Reglas que interpretan nombres de ítem de CS2 (CLEAN-10). Funciones puras: cada
prefijo («StatTrak™ », «★ », «Souvenir ») y la marca de slab viven solo aquí.

Invariante (CLAUDE.md): el `name` de una tarjeta sale de `markethashname` antes que de
`marketname`, porque el marketname puede venir localizado. Estas reglas reciben ese name.
"""
from collections.abc import Sequence

from steam.domain.catalog import WEAR_NAMES

STATTRAK = "StatTrak™ "
STAR = "★ "
SOUVENIR = "Souvenir "
_SLAB = "sticker slab"


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


def is_sticker_slab(name: str) -> bool:
    return _SLAB in name.lower()


def skin_base(name: str) -> str:
    """Nombre sin el desgaste: 'AK-47 | Crane Flight (Field-Tested)' → sin '(...)'."""
    return name.split(" (")[0].strip().lower()
