"""CLEAN-08: los TypedDict de steam/domain/models.py son el contrato con el front escrito
como tipo. Sus claves tienen que ser exactamente las que fijan los tests de contrato
de CAL-09: si alguien añade una clave al mapper sin tocar el modelo (o al revés), mypy
deja de proteger el JSON y este test lo dice.
"""
from steam.adapters.steam_adapter import adapt_item
from steam.domain.models import (
    HistoryPoint, IndexPoint, TopMover, MarketIndexPoint, MarketProvider, MoverItem, NewsItem, RankedCard, RankingRow,
    SkinCard,
)
from steam.mappers.items import _map_item
from steam.mappers.market_index import _map_market_index_point
from steam.mappers.movers import _map_topmovers_item
from steam.mappers.news import _map_news_item
from steam.mappers.rows import _row_to_item, _to_row
from tests.test_steam_contract_items import HISTORY_KEYS, NEWS_KEYS
from tests.test_steam_contract_market import SKIN_CARD_KEYS
from tests.test_steam_contract_rows import ITEM_KEYS, SAMPLE


def _keys(td) -> set[str]:
    return set(td.__required_keys__) | set(td.__optional_keys__)


def test_modelos_y_contrato_tienen_las_mismas_claves():
    assert _keys(SkinCard) == SKIN_CARD_KEYS
    assert _keys(RankedCard) == ITEM_KEYS
    assert _keys(NewsItem) == NEWS_KEYS
    assert _keys(HistoryPoint) == HISTORY_KEYS
    assert _keys(MarketIndexPoint) == {"date", "price", "change", "volume"}
    assert _keys(MarketProvider) == {"id", "name", "logoUrl"}
    assert _keys(MoverItem) == SKIN_CARD_KEYS | {"_change24h"}
    assert MoverItem.__optional_keys__ == frozenset({"_change24h"})


def test_cada_mapper_emite_las_claves_de_su_modelo():
    assert set(_map_item(adapt_item({"markethashname": "x"}))) == _keys(SkinCard)
    assert set(_map_topmovers_item(TopMover(adapt_item({"markethashname": "x"}), 1.0))) == _keys(MoverItem)
    assert set(_map_market_index_point(IndexPoint("", None, None, None))) == _keys(MarketIndexPoint)
    assert set(_map_news_item({}, 0)) == _keys(NewsItem)
    assert set(_row_to_item(_to_row(SAMPLE, 0))) == _keys(RankedCard)
    assert set(_to_row(SAMPLE, 0, "hot")) == _keys(RankingRow)
    assert set(_to_row(SAMPLE, 0)) == _keys(RankingRow) - {"bucket"}
