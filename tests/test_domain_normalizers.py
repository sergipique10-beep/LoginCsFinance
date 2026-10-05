"""CLEAN-10 / CLEAN-17: normalizadores de nombres e imágenes (steam/domain/normalizers.py,
ex names.py), steam/utils/urls.py y el catálogo de constantes (steam/domain/catalog.py).
Tabla con nombres reales: candidatos de imagen en el orden de hoy, claves de catálogo,
souvenir, marca de slab y skin base. Al final, la guardia: los prefijos y marcas solo
pueden aparecer en `domain/`.
"""
import re
from pathlib import Path

import pytest

from steam.domain import catalog
from steam.domain.normalizers import (
    catalog_keys_for_skin, has_slab_mark, image_lookup_candidates, name_key, names_match,
    normalize_image_url, skin_base, without_souvenir,
)
from steam.utils.urls import STEAM_CDN, is_http_url, steam_cdn_url

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


@pytest.mark.parametrize("text, slab", [
    (SLAB, True), ("sticker slab | x", True), ("Sticker Slab", True), (AGENT, False), ("", False), (None, False),
])
def test_has_slab_mark(text, slab):
    assert has_slab_mark(text) is slab


def test_name_key_y_names_match():
    assert name_key("AK-47 | Redline (Field-Tested)") == "ak-47 | redline (field-tested)"
    assert names_match("AK-47 | Redline", "ak-47 | redline")
    assert not names_match("AK-47 | Redline", "AK-47 | Redline (Field-Tested)")


@pytest.mark.parametrize("raw, expected", [
    ("", ""), (None, ""),
    ("https://cdn/x.png", "https://cdn/x.png"),
    ("http://cdn/x.png", "http://cdn/x.png"),
    ("/economy/image/abc", f"{STEAM_CDN}/economy/image/abc"),
    ("abc123", f"{STEAM_CDN}/economy/image/abc123"),
])
def test_normalize_image_url(raw, expected):
    assert normalize_image_url(raw) == expected


def test_utils_urls():
    assert is_http_url("https://a") and is_http_url("http://a") and not is_http_url("/economy/image/a")
    assert steam_cdn_url("/economy/image/a") == f"{STEAM_CDN}/economy/image/a"
    assert steam_cdn_url("a") == f"{STEAM_CDN}/economy/image/a"


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
    """Los prefijos («StatTrak™ », «★ », «Souvenir »), la marca de slab (nombre o
    `itemtype`) y las marcas de respaldo de categoría ("glove", "knife", "bayonet")
    solo pueden aparecer en steam/domain/ (CLEAN-10, ampliada en CLEAN-17)."""
    pattern = re.compile(r'StatTrak™ |"★ |Souvenir |sticker slab|"glove"|"knife"|"bayonet"', re.IGNORECASE)
    offenders = [
        f"{p.relative_to(ROOT)}:{n}"
        for d in ("steam", "tools")
        for p in (ROOT / d).rglob("*.py")
        if "domain" not in p.relative_to(ROOT).parts
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []
