"""Enumeraciones del dominio de steam/.

`FetchStatus` y `Served` son `Literal` y no `Enum` a propósito: `Fetched.status` y
`log_degraded` se comparan y se escriben como strings en services, rutas y tests, y un
`Enum` obligaría a tocar todos los sitios sin cambiar nada observable. Lo que importa
es que las dos listas vivan en un solo sitio y que `served_to_status`
(`steam/errors/handling.py`) sea la única correspondencia entre ellas.

`Market`, `WeaponCategory`, `Wear` y `NewsCategory` son `str, Enum`: los
valores son los strings que ya viajaban sueltos por el código, y `domain/catalog.py`
deriva sus tuplas y conjuntos de aquí. Fuera de `domain/` se usan los `.value`: en
Python ≥ 3.11 `f"{Market.BUFF}"` da `Market.BUFF`, no `buff`, y una URL o una clave
de caché construida así cambiaría en silencio.
"""
from enum import Enum
from typing import Literal

# Estado de un `Fetched[T]`: `ok` (dato bueno), `stale` (caché caducada), `partial`
# (respaldo incompleto: topmovers, proveedores estáticos) o `error` (vacío o nada).
FetchStatus = Literal["ok", "partial", "stale", "error"]

# Qué se sirvió en una degradación, para la línea `[steam-degraded] served=`.
Served = Literal["stale", "empty", "fallback", "error"]


class Market(str, Enum):
    """Mercados con cliente propio en `steam/api/` (`steam` va por la ruta legacy)."""
    STEAM = "steam"
    CSFLOAT = "csfloat"
    BUFF = "buff"


class WeaponCategory(str, Enum):
    """Categoría de alto nivel por la que agrupa el filtro del frontend (`weaponType`).
    Son las que produce `catalog.weapon_category` desde la tabla y sus respaldos; el
    último recurso (`itemtype.title()`) queda fuera del enum a propósito."""
    RIFLE = "Rifle"
    SNIPER_RIFLE = "Sniper Rifle"
    PISTOL = "Pistol"
    SMG = "SMG"
    HEAVY = "Heavy"
    KNIFE = "Knife"
    GLOVES = "Gloves"
    STICKER = "Sticker"
    AGENT = "Agent"


class Wear(str, Enum):
    """Los cinco desgastes de CS2, en orden de menor a mayor."""
    FACTORY_NEW = "Factory New"
    MINIMAL_WEAR = "Minimal Wear"
    FIELD_TESTED = "Field-Tested"
    WELL_WORN = "Well-Worn"
    BATTLE_SCARRED = "Battle-Scarred"


class NewsCategory(str, Enum):
    """Fuente de una noticia de Steam News, con el color de su chip (`categoryColor`).

    El color es el que `_map_news_item` asignaba por substring del `feedname`: azul para
    lo oficial (blog, Valve), morado para la escena competitiva, ámbar para el resto.
    """
    color: str

    def __new__(cls, value: str, color: str) -> "NewsCategory":
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.color = color
        return obj

    BLOG = ("blog", "4a9eff")
    VALVE = ("valve", "4a9eff")
    HLTV = ("hltv", "8847ff")
    LIQUIPEDIA = ("liquipedia", "8847ff")
    ESPORTS = ("esports", "8847ff")
    OTHER = ("other", "f0c040")
