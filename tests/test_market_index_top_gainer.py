"""UX-35: `hottestItem` de /market/index es el mayor *gainer* de topmovers y su
`change24h` es el porcentaje de variación del precio en 24 h.

La muestra es una respuesta real de steamwebapi (`/market-index/cs2`, 2026-10-02).
Un sticker de 0,17 $ no puede haber subido 450 $: es un +450 %. El docstring de
`_map_topmovers_item` decía que era un importe y el front lo rotulaba «% Vol»."""
import asyncio
from types import SimpleNamespace

import pytest

from steam.routes import market
from steam.adapters.static_catalog_adapter import adapt_catalog_source
from steam.adapters.steam_adapter import adapt_market_index
from steam.mappers.movers_mapper import _build_movers_from_topmovers
from steam.services.catalog import _register_flat, _register_skin, rarity_from_cache as _rarity_from_cache
from stores import (
    _image_cache_meta, _item_image_cache, _item_rarity_cache, _market_index_cache, _topmovers_raw_cache,
)

TOPMOVERS = {
    "gainers": [
        {"markethashname": "Sticker | Run Boost Lift Kits", "change24h": 450, "price": 0.17},
        {"markethashname": "Sticker | zorte (Gold, Ranked) | Cologne 2026", "change24h": 378.21, "price": 190},
        {"markethashname": "Souvenir Charm | Budapest 2025 Highlight | b1t mid-air catch", "change24h": 277.78, "price": 0.34},
    ],
    "losers": [
        {"markethashname": "Souvenir Charm | Cologne 2026 Highlight | MATYS ACE", "change24h": -52.17, "price": 0.27},
        {"markethashname": "Souvenir MAC-10 | Tornado (Battle-Scarred)", "change24h": -49.78, "price": 23.93},
        {"markethashname": "Dual Berettas | Retribution (Factory New)", "change24h": -37.71, "price": 6.99},
    ],
}


class _Resp:
    status_code = 200

    def json(self):
        return {"history": [], "topmovers": TOPMOVERS,
                "turnover24h": 2237753.25, "sold24h": 1214016}


class _Client:
    async def get(self, *_args, **_kwargs):
        return _Resp()


# Entradas reales del catálogo de ByMykel (stickers.json y skins.json, 2026-10-02).
STICKER = {
    "name": "Sticker | Run Boost Lift Kits", "market_hash_name": "Sticker | Run Boost Lift Kits",
    "image": "https://example.test/sticker.png",
    "rarity": {"id": "rarity_rare", "name": "High Grade", "color": "#4b69ff"},
}
SKIN = {
    "name": "MAC-10 | Tornado", "image": "https://example.test/mac10.png", "stattrak": False,
    "wears": [{"name": "Battle-Scarred"}],
    "rarity": {"id": "rarity_common_weapon", "name": "Consumer Grade", "color": "#b0c3d9"},
}


@pytest.fixture
def catalogo():
    """Catálogo estático con dos entradas y marcado como recién cargado, para que
    /market/index no salga a la red a por el de verdad."""
    import time
    saved = (dict(_item_image_cache), dict(_item_rarity_cache), dict(_image_cache_meta))
    _item_image_cache.clear(); _item_rarity_cache.clear()
    _register_flat(adapt_catalog_source([STICKER], label="stickers")[0])
    _register_skin(adapt_catalog_source([SKIN], label="skins")[0])
    _image_cache_meta.put("catalog", 1)
    yield
    for store, old in zip((_item_image_cache, _item_rarity_cache, _image_cache_meta), saved):
        store.clear(); store.update(old)


def _index():
    _market_index_cache.clear()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http_client=_Client())))
    try:
        return asyncio.run(market.get_market_index(request, tf="24h", user={}))
    finally:
        _market_index_cache.clear()
        _topmovers_raw_cache.clear()


def test_hottest_item_es_el_mayor_gainer_con_su_porcentaje(catalogo):
    result = _index()

    # UX-38: el precio viaja con el porcentaje para que la card lo ponga en contexto.
    # UX-39: y la rareza, que topmovers no trae, sale del catálogo estático.
    assert result["hottestItem"] == {
        "name": "Sticker | Run Boost Lift Kits", "change24h": 450.0, "price": 0.17,
        "rarity": "High Grade", "rarityColor": "4b69ff",
    }
    assert result["sold24h"] == 1214016


def test_hottest_item_sin_rareza_si_no_esta_en_el_catalogo(catalogo):
    _item_rarity_cache.clear()
    hottest = _index()["hottestItem"]
    assert hottest["rarity"] is None and hottest["rarityColor"] is None
    assert hottest["price"] == 0.17


def test_rareza_del_catalogo_cubre_desgastes_y_souvenir(catalogo):
    assert _rarity_from_cache("MAC-10 | Tornado (Battle-Scarred)") == ("Consumer Grade", "b0c3d9")
    assert _rarity_from_cache("Souvenir MAC-10 | Tornado (Battle-Scarred)") == ("Consumer Grade", "b0c3d9")
    assert _rarity_from_cache("Sticker Slab | Mood Ring Strafe (Holo)") is None
    # El caché de imágenes no cambia de forma por registrar la rareza.
    assert _item_image_cache["MAC-10 | Tornado (Battle-Scarred)"] == SKIN["image"]
    assert _item_image_cache["★ MAC-10 | Tornado"] == SKIN["image"]


def test_change24h_es_un_porcentaje_no_un_importe():
    # Un importe de caída no puede superar el precio; aquí lo supera en todos los
    # perdedores (−52,17 con precio 0,27) y ninguno baja de −100: es un porcentaje.
    for loser in TOPMOVERS["losers"]:
        assert -100 <= loser["change24h"] < 0
    assert any(abs(l["change24h"]) > l["price"] for l in TOPMOVERS["losers"])

    mi = adapt_market_index({"history": [], "topmovers": TOPMOVERS})
    movers = _build_movers_from_topmovers(mi.gainers, mi.losers)
    assert movers["hot"][0]["name"] == "Sticker | Run Boost Lift Kits"
    assert movers["cold"][0]["name"] == "Souvenir Charm | Cologne 2026 Highlight | MATYS ACE"
