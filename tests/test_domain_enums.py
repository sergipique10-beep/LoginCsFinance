"""CLEAN-17 (4.1): los enums de steam/domain/enums.py y las constantes de catalog.py que
se derivan de ellos. Lo que se fija es que los VALORES sean los strings de siempre: son
los que viajan en el JSON (`weaponType`, `exterior`), en las URLs de steamwebapi y en
las claves de caché.
"""
import json

import pytest

from steam.domain import catalog
from steam.domain.enums import Market, NewsCategory, Wear, WeaponCategory


def test_market_values_y_derivados_de_catalog():
    assert [m.value for m in Market] == ["steam", "csfloat", "buff"]
    assert catalog.TRACKED_MARKETS == ("csfloat", "buff")
    assert catalog.HISTORY_MARKETS == frozenset({"buff", "csfloat"})
    assert catalog.PROVIDER_IDS == frozenset({"csfloat", "buff"})
    assert set(catalog.KNOWN_LOGOS) == {"steam", "csfloat", "buff"}
    # VALID_MARKETS = los de histórico + los passthrough, y sigue siendo el mismo conjunto.
    assert catalog.VALID_MARKETS == frozenset({
        "buff", "skinport", "skinbaron", "dmarket", "waxpeer", "bitskins", "csgotm",
        "haloskins", "tradeit", "skinbid", "csfloat", "youpin",
    })
    assert catalog.HISTORY_MARKETS.isdisjoint(catalog.PASSTHROUGH_MARKETS)
    assert "steam" not in catalog.VALID_MARKETS   # Steam va por la ruta legacy sin market


def test_los_derivados_son_str_planos_no_miembros():
    """Fuera de domain/ se formatean en URLs y claves: en Python ≥ 3.11 `f"{Market.BUFF}"`
    es `Market.BUFF`, así que catalog.py expone `.value`."""
    assert all(type(m) is str for m in catalog.TRACKED_MARKETS)
    assert all(type(m) is str for m in catalog.VALID_MARKETS)
    assert all(type(w) is str for w in catalog.WEAR_NAMES)
    assert type(catalog.weapon_category("ak-47")) is str
    assert f"{Market.BUFF.value}" == "buff"


def test_wear_en_orden():
    assert catalog.WEAR_NAMES == ("Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred")
    assert tuple(Wear) == tuple(Wear(w) for w in catalog.WEAR_NAMES)


def test_weapon_category_tabla_y_respaldos_sobre_el_enum():
    assert set(catalog.WEAPON_CATEGORY.values()) == {
        WeaponCategory.RIFLE, WeaponCategory.SNIPER_RIFLE, WeaponCategory.PISTOL,
        WeaponCategory.SMG, WeaponCategory.HEAVY, WeaponCategory.KNIFE,
    }
    assert catalog.weapon_category("awp") == WeaponCategory.SNIPER_RIFLE == "Sniper Rifle"
    assert catalog.weapon_category("sport gloves") == WeaponCategory.GLOVES.value
    assert catalog.weapon_category("sticker capsule") == WeaponCategory.STICKER.value
    assert catalog.weapon_category("special agent") == WeaponCategory.AGENT.value
    # El último recurso no es categoría del enum: itemtype capitalizado.
    assert catalog.weapon_category("music kit") == "Music Kit"
    assert "Music Kit" not in {c.value for c in WeaponCategory}


@pytest.mark.parametrize("category, color", [
    (NewsCategory.BLOG, "4a9eff"), (NewsCategory.VALVE, "4a9eff"),
    (NewsCategory.HLTV, "8847ff"), (NewsCategory.LIQUIPEDIA, "8847ff"), (NewsCategory.ESPORTS, "8847ff"),
    (NewsCategory.OTHER, "f0c040"),
])
def test_news_category_lleva_su_color(category, color):
    assert category.color == color
    assert NewsCategory(category.value) is category   # el color no forma parte de la identidad


def test_str_enum_serializa_como_su_valor():
    assert json.dumps({"m": Market.CSFLOAT, "w": Wear.FIELD_TESTED, "c": WeaponCategory.KNIFE}) == \
        '{"m": "csfloat", "w": "Field-Tested", "c": "Knife"}'
