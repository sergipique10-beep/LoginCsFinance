"""CLEAN-10: reglas de nombres de ítem (steam/domain/names.py) y catálogo de constantes
(steam/domain/catalog.py). Tabla con nombres reales: candidatos de imagen en el orden
de hoy, claves de catálogo, souvenir, slab y skin base.
"""
import re
from pathlib import Path

import pytest

from steam.domain import catalog
from steam.domain.names import (
    catalog_keys_for_skin, image_lookup_candidates, is_sticker_slab, skin_base, without_souvenir,
)

ROOT = Path(__file__).resolve().parent.parent

KARAMBIT_ST = "★ StatTrak™ Karambit | Doppler (Factory New)"
KARAMBIT = "★ Karambit | Doppler (Factory New)"
AK_ST = "StatTrak™ AK-47 | Redline (Field-Tested)"
AWP_SOUVENIR = "Souvenir AWP | Dragon Lore (Factory New)"
CHARM_SOUVENIR = "Souvenir Charm | Budapest 2025 Highlight | b1t mid-air catch"
SLAB = "Sticker Slab | Crown (Foil)"
AGENT = "Sir Bloody Miami Darryl | The Professionals"
PATCH = "Patch | Phoenix"


@pytest.mark.parametrize("name, item_type, candidates", [
    # El ★ se quita del nombre original, no del que ya perdió el StatTrak™: es el orden de hoy.
    (KARAMBIT_ST, "Karambit", [KARAMBIT_ST, KARAMBIT, "StatTrak™ Karambit | Doppler (Factory New)"]),
    (KARAMBIT, "Karambit", [KARAMBIT, "Karambit | Doppler (Factory New)"]),
    (AK_ST, "AK-47", [AK_ST, "AK-47 | Redline (Field-Tested)"]),
    (AWP_SOUVENIR, "AWP", [AWP_SOUVENIR, "AWP | Dragon Lore (Factory New)"]),
    (CHARM_SOUVENIR, "Charm", [CHARM_SOUVENIR]),   # un charm souvenir no es una skin souvenir
    (CHARM_SOUVENIR, None, [CHARM_SOUVENIR, CHARM_SOUVENIR[len("Souvenir "):]]),
    (SLAB, "Sticker Slab", [SLAB]),
    (AGENT, "Agent", [AGENT]),
    (PATCH, "Patch", [PATCH]),
])
def test_image_lookup_candidates(name, item_type, candidates):
    assert image_lookup_candidates(name, item_type) == candidates


def test_catalog_keys_for_skin():
    assert catalog_keys_for_skin("AK-47 | Redline", ["Field-Tested"], stattrak=True) == [
        "AK-47 | Redline", "★ AK-47 | Redline",
        "StatTrak™ AK-47 | Redline", "★ StatTrak™ AK-47 | Redline",
        "AK-47 | Redline (Field-Tested)", "★ AK-47 | Redline (Field-Tested)",
        "StatTrak™ AK-47 | Redline (Field-Tested)", "★ StatTrak™ AK-47 | Redline (Field-Tested)",
    ]
    # Sin desgastes en el catálogo, los cinco de siempre.
    keys = catalog_keys_for_skin("Karambit | Doppler", [], stattrak=False)
    assert keys[:2] == ["Karambit | Doppler", "★ Karambit | Doppler"]
    assert keys[2:] == [f"{b} ({w})" for b in keys[:2] for w in catalog.WEAR_NAMES]


@pytest.mark.parametrize("name, base", [
    (AWP_SOUVENIR, "AWP | Dragon Lore (Factory New)"), (AK_ST, None), ("Souvenir", None),
])
def test_without_souvenir(name, base):
    assert without_souvenir(name) == base


@pytest.mark.parametrize("name, slab", [(SLAB, True), ("sticker slab | x", True), (AGENT, False), ("", False)])
def test_is_sticker_slab(name, slab):
    assert is_sticker_slab(name) is slab


def test_skin_base():
    assert skin_base("AK-47 | Crane Flight (Field-Tested)") == skin_base("AK-47 | Crane Flight (Minimal Wear)")
    assert skin_base("Glove Case") == "glove case"


def test_catalogo_inmutable_y_respaldo_por_copia():
    first = catalog.fallback_providers()
    first[0]["name"] = "mutado"
    assert catalog.fallback_providers()[0]["name"] == "Steam"
    assert [p["id"] for p in catalog.fallback_providers()] == ["steam", "csfloat", "buff"]
    with pytest.raises(TypeError):
        catalog.KNOWN_LOGOS["steam"] = "x"   # type: ignore[index]
    assert catalog.TRACKED_MARKETS == ("csfloat", "buff")
    assert catalog.HISTORY_MARKETS <= catalog.VALID_MARKETS
    assert catalog.weapon_category("ak-47") == "Rifle"


def test_reglas_de_nombres_solo_en_domain():
    pattern = re.compile(r'StatTrak™ |"★ |Souvenir |sticker slab', re.IGNORECASE)
    offenders = [
        f"{p.relative_to(ROOT)}:{n}"
        for d in ("steam", "tools")
        for p in (ROOT / d).rglob("*.py")
        if "domain" not in p.relative_to(ROOT).parts
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []
