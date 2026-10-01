"""UX-35: `hottestItem` de /market/index es el mayor *gainer* de topmovers y su
`change24h` es el porcentaje de variación del precio en 24 h.

La muestra es una respuesta real de steamwebapi (`/market-index/cs2`, 2026-10-02).
Un sticker de 0,17 $ no puede haber subido 450 $: es un +450 %. El docstring de
`_map_topmovers_item` decía que era un importe y el front lo rotulaba «% Vol»."""
import asyncio
from types import SimpleNamespace

from steam.routes import market
from steam.services import _build_movers_from_topmovers

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
        return {"history": [], "changes": {"24h": None}, "topmovers": TOPMOVERS,
                "turnover24h": 2237753.25, "sold24h": 1214016}


class _Client:
    async def get(self, *_args, **_kwargs):
        return _Resp()


def test_hottest_item_es_el_mayor_gainer_con_su_porcentaje():
    market._market_index_cache.clear()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http_client=_Client())))
    try:
        result = asyncio.run(market.get_market_index(request, tf="24h", user={}))
    finally:
        market._market_index_cache.clear()
        market._topmovers_raw_cache.clear()

    assert result["hottestItem"] == {"name": "Sticker | Run Boost Lift Kits", "change24h": 450.0}
    assert result["sold24h"] == 1214016


def test_change24h_es_un_porcentaje_no_un_importe():
    # Un importe de caída no puede superar el precio; aquí lo supera en todos los
    # perdedores (−52,17 con precio 0,27) y ninguno baja de −100: es un porcentaje.
    for loser in TOPMOVERS["losers"]:
        assert -100 <= loser["change24h"] < 0
    assert any(abs(l["change24h"]) > l["price"] for l in TOPMOVERS["losers"])

    movers = _build_movers_from_topmovers(TOPMOVERS["gainers"], TOPMOVERS["losers"])
    assert movers["hot"][0]["name"] == "Sticker | Run Boost Lift Kits"
    assert movers["cold"][0]["name"] == "Souvenir Charm | Cologne 2026 Highlight | MATYS ACE"
