"""CLEAN-17: reglas de negocio de steam/domain/rules.py. Ninguna regla es nueva: cada
caso fija el comportamiento que ya tenían sueltas en validators, services/market y
news_mapper. Lo nuevo es `is_sticker_slab(item)` por `item_type` y el filtro de slabs
en el trending (CAL-14).
"""
import pytest

from steam.adapters.news_adapter import adapt_news_entry
from steam.adapters.steam_adapter import adapt_item
from steam.domain import catalog, rules
from steam.domain.enums import NewsCategory, WeaponCategory


# ── Plausibilidad (ex validators) ─────────────────────────────────────────────

@pytest.mark.parametrize("new, old, ok", [
    (10.0, 10.0, True), (10.0, 1.0, True), (10.0, 100.0, True),   # bordes inclusivos (10×)
    (10.0, 0.99, False), (10.0, 100.1, False),
])
def test_plausible_ratio(new, old, ok):
    assert rules.plausible_ratio(new, old) is ok


@pytest.mark.parametrize("rate, ok", [
    (0.88, True), (1, True), (0.5, False), (2.0, False), (0.0, False), (None, False), ("0.88", False),
])
def test_plausible_fx_rate(rate, ok):
    assert rules.plausible_fx_rate(rate) is ok


# ── Rankings ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("price, sold, min_sold, ok", [
    (10.80, 5, rules.MIN_SOLD_MOVERS, True), (10.79, 500, rules.MIN_SOLD_MOVERS, False),
    (50.0, 4, rules.MIN_SOLD_MOVERS, False), (50.0, 1, rules.MIN_SOLD_TRENDING, True),
    (None, None, rules.MIN_SOLD_TRENDING, False),
])
def test_ranking_eligible(price, sold, min_sold, ok):
    assert rules.ranking_eligible(adapt_item({"pricelatestsell": price, "sold24h": sold}), min_sold) is ok


@pytest.mark.parametrize("raw, price", [
    ({"pricelatestsell": 5, "pricelatest": 7}, 5.0),
    ({"pricelatestsell": 0, "pricelatest": "7.5"}, 7.5),
    ({"pricelatestsell": "x", "pricemedian": 3}, 3.0),
    ({}, None),
])
def test_canonical_price(raw, price):
    """Sobre el modelo interno desde CLEAN-18 (antes, el dict crudo de /item)."""
    assert rules.canonical_price(adapt_item(raw)) == price
    assert rules.canonical_price(None) is None


def test_turnover_es_precio_por_unidades():
    assert rules.turnover({"priceLatest": 28.0, "sold24h": 30}) == 840.0
    assert rules.turnover({}) == 0 and rules.turnover({"priceLatest": None, "sold24h": None}) == 0


def test_diversificar_reparte_y_rellena():
    items = [{"name": f"AK-47 | S{i} (FT)", "weaponType": "Rifle"} for i in range(6)] + \
            [{"name": "AWP | X (FT)", "weaponType": "Sniper Rifle"}]
    r = rules.diversificar(items, 6)
    assert [i["weaponType"] for i in r[:5]] == ["Rifle"] * 4 + ["Sniper Rifle"]
    assert len(r) == 6   # el hueco sobrante se rellena con un rifle descartado por cuota
    assert rules.diversificar([], 5) == []


# ── Slabs ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, slab", [
    # Por item_type primero: el fixture de steamwebapi trae «Sticker Slab».
    ({"markethashname": "X", "itemtype": "Sticker Slab"}, True),
    ({"markethashname": "X", "itemtype": "sticker slab"}, True),
    # Si el itemtype no lo dice (falta, o «sticker» a secas como en el contrato de los
    # ticks), decide el nombre: canónico o localizado.
    ({"markethashname": "Sticker Slab | Crown (Foil)", "itemtype": "sticker"}, True),
    ({"markethashname": "Sticker Slab | Crown (Foil)"}, True),
    ({"marketname": "Sticker Slab | X", "markethashname": None}, True),
    ({"markethashname": "Sticker | Crown (Foil)", "itemtype": "sticker"}, False),
    ({"markethashname": "AK-47 | Redline (Field-Tested)", "itemtype": "rifle"}, False),
])
def test_is_sticker_slab_por_item_type_y_luego_nombre(raw, slab):
    assert rules.is_sticker_slab(adapt_item(raw)) is slab


# ── Categoría de arma: tabla de respaldo explícita ────────────────────────────

def test_weapon_category_fallback_es_una_tabla_ordenada():
    assert [m for m, _ in catalog.WEAPON_CATEGORY_FALLBACK] == [
        "glove", "knife", "bayonet", "daggers", "karambit", "sticker", "agent", "operator",
    ]
    assert all(isinstance(c, WeaponCategory) for _, c in catalog.WEAPON_CATEGORY_FALLBACK)


@pytest.mark.parametrize("itemtype, category", [
    ("Sport Gloves", "Gloves"), ("Hand Wraps", "Hand Wraps"),   # «wraps» no está en la tabla: title()
    ("shadow daggers", "Knife"), ("Talon Knife", "Knife"), ("M9 Bayonet", "Knife"), ("karambit", "Knife"),
    ("sticker capsule", "Sticker"), ("special agent", "Agent"), ("Operator", "Agent"),
    ("music kit", "Music Kit"), ("", None), (None, None),
])
def test_weapon_category_respaldos(itemtype, category):
    assert catalog.weapon_category(itemtype) == category


def test_weapon_type_del_item_gana_siempre():
    """Cuando steamwebapi trae weapontype, la categoría derivada del itemtype no se mira
    (`_map_item`: `item.weapon_type or weapon_category(item.item_type)`)."""
    from steam.mappers.item_mapper import _map_item
    assert _map_item(adapt_item({"markethashname": "X", "weapontype": "Custom", "itemtype": "knife"}))["weaponType"] == "Custom"
    assert _map_item(adapt_item({"markethashname": "X", "itemtype": "knife"}))["weaponType"] == "Knife"


# ── Noticias ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title, readable", [
    ("Counter-Strike 2 Update Released", True),
    ("Обновление Counter-Strike 2 вышло сегодня", False),
    ("反恐精英2更新已经发布", False),
    ("Team Spirit signs new player from Москва roster today", True),   # una palabra no descarta
    ("", True), ("2026 — 100% (!!)", True),
])
def test_is_readable_news(title, readable):
    assert rules.is_readable_news(adapt_news_entry({"title": title})) is readable


@pytest.mark.parametrize("feedname, feedlabel, category", [
    ("Counter-Strike Blog", None, NewsCategory.BLOG),
    ("valve_news", None, NewsCategory.VALVE),
    ("HLTV.org", None, NewsCategory.HLTV),
    ("Liquipedia", None, NewsCategory.LIQUIPEDIA),
    ("Esports.gg", None, NewsCategory.ESPORTS),
    ("steam_community_announcements", "Community Announcements", NewsCategory.OTHER),
    (None, "HLTV", NewsCategory.HLTV),          # feedlabel solo si no hay feedname
    ("Dexerto", "HLTV", NewsCategory.OTHER),    # ...y nunca por encima de él
    (None, None, NewsCategory.OTHER),
])
def test_news_category_tabla_explicita(feedname, feedlabel, category):
    assert rules.news_category(feedname, feedlabel) is category


def test_news_category_mismos_colores_que_antes():
    """El chip del front: azul lo oficial, morado la escena, ámbar el resto."""
    assert rules.news_category("blog").color == rules.news_category("valve").color == "4a9eff"
    assert rules.news_category("hltv").color == rules.news_category("esport").color == "8847ff"
    assert rules.news_category("dexerto").color == "f0c040"
