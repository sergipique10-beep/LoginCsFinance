"""CLEAN-14: los adapters (`steam/adapters/`) convierten el JSON crudo de cada fuente en
el modelo interno. Payload normal (fixture), incompleto (campo ausente → None),
implausible (tipo imposible → InvalidField), forma incorrecta (→ UnexpectedPayload),
variantes de nombre raras, provider sin metadata, noticia vacía."""
import pytest

from steam.adapters import (
    buff_adapter, csfloat_adapter, fx_adapter, news_adapter, provider_adapter, static_catalog_adapter,
    steam_adapter,
)
from steam.adapters._common import history_points, require_dict, require_list
from steam.domain.models import SteamItem
from steam.domain.validators import as_bool, as_float, as_int, as_str
from steam.errors import InvalidField, UnexpectedPayload


# ── validadores de valor ──────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), ("abc", None), (0, 0.0), ("0", 0.0), (3, 3.0), ("2.5", 2.5), (True, None),
])
def test_as_float(value, expected):
    assert as_float(value) == expected


def test_as_int_as_bool_as_str():
    assert as_int("7.9") == 7 and as_int(None) is None and as_int(0) == 0
    assert as_bool(0) is False and as_bool(None) is None and as_bool("x") is True
    assert as_str(123) == "123" and as_str(None) is None and as_str("") == ""


@pytest.mark.parametrize("fn", [as_float, as_int, as_bool, as_str])
def test_un_contenedor_es_invalid_field(fn):
    with pytest.raises(InvalidField) as exc:
        fn({"x": 1}, field="price", source="steamwebapi", op="items")
    assert exc.value.field == "price" and "steamwebapi.items" in str(exc.value)


# ── forma ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn, raw", [
    (steam_adapter.adapt_items, {"a": 1}), (steam_adapter.adapt_items, "texto"), (steam_adapter.adapt_items, [1, 2]),
    (steam_adapter.adapt_inventory, None), (steam_adapter.adapt_profile, [1]),
    (steam_adapter.adapt_market_index, "x"), (steam_adapter.adapt_market_index, {"history": "x"}),
    (steam_adapter.adapt_market_index, {"topmovers": []}), (steam_adapter.adapt_market_index, {"topmovers": {"gainers": {}}}),
    (lambda r: steam_adapter.adapt_price_rows(r, market="buff"), {}),
    (csfloat_adapter.adapt_history, {"error": "x"}), (buff_adapter.adapt_history, [[]]),
    (news_adapter.adapt_news, []), (news_adapter.adapt_news, {"appnews": []}), (news_adapter.adapt_news, {"appnews": {"newsitems": "x"}}),
    (news_adapter.adapt_news, {"appnews": {"newsitems": {}}}),
    (lambda r: static_catalog_adapter.adapt_catalog_source(r, label="skins"), {}),
    (provider_adapter.adapt_markets, {"id": "x"}), (fx_adapter.adapt_rates, []),
])
def test_forma_incorrecta_es_unexpected_payload(fn, raw):
    with pytest.raises(UnexpectedPayload):
        fn(raw)


def test_require_helpers_nombran_fuente_y_operacion():
    with pytest.raises(UnexpectedPayload, match="steamwebapi.items expected a list, got dict"):
        require_list({}, source="steamwebapi", op="items")
    with pytest.raises(UnexpectedPayload, match="expected an object"):
        require_dict([], source="s", op="o")


# ── items ─────────────────────────────────────────────────────────────────────

def test_items_fixture(payload):
    ak, knife, slab = steam_adapter.adapt_items(payload("steamwebapi/items"))
    assert ak.name == "AK-47 | Redline (Field-Tested)" and ak.latest_price == 20.0
    assert ak.prices[1].market == "csfloat" and ak.prices[1].quantity == 40
    assert ak.sold_24h == 50 and ak.buy_order_price == 18.5 and ak.marketable is True
    # Variante con nombre inusual: prefijos, fase y campos nulos/cero conservados.
    assert knife.is_star and knife.is_stattrak and knife.variants[0].phase == "Phase 2"
    assert knife.sold_24h is None            # ausente → None, no 0
    assert knife.buy_order_price == 0.0      # 0 es un dato: «nadie compra»
    assert knife.price_real_7d is None and knife.price_real_30d == 0.22
    assert slab.item_type == "Sticker Slab" and slab.image == ""


def test_item_incompleto_todo_none():
    item = steam_adapter.adapt_item({})
    assert isinstance(item, SteamItem) and item.name == "" and item.latest_price is None
    assert item.sold_24h is None and item.prices == () and item.variants == () and item.marketable is None


def test_item_con_tipo_imposible_es_invalid_field():
    with pytest.raises(InvalidField) as exc:
        steam_adapter.adapt_item({"markethashname": "AK", "pricelatestsell": {"usd": 1}})
    assert exc.value.field == "pricelatestsell"
    with pytest.raises(UnexpectedPayload):
        steam_adapter.adapt_item({"prices": "no-es-lista"})


def test_item_precio_no_numerico_es_none_no_cero():
    item = steam_adapter.adapt_item({"pricelatestsell": "n/a", "price": "12.5", "sold24h": "x"})
    assert item.price_latest_sell is None and item.latest_price == 12.5 and item.sold_24h is None


def test_inventory_anidado_y_plano(payload):
    nested, flat = steam_adapter.adapt_inventory(payload("steamwebapi/inventory"))
    assert nested.asset_id == "A1" and nested.float_value == 0.2345 and nested.name.startswith("AK-47")
    assert flat.market_hash_name == "Solitude (Field-Tested)"      # markethashname gana al marketname localizado
    assert flat.market_name == "Solidão (Testada em Campo)"


def test_market_hash_name_con_guion_bajo_tambien_vale():
    assert steam_adapter.adapt_item({"market_hash_name": "AK"}).name == "AK"


# ── perfil ────────────────────────────────────────────────────────────────────

def test_profile(payload):
    p = steam_adapter.adapt_profile(payload("steamwebapi/profile"))
    assert p.persona_name == "Marc" and p.persona_state == 1
    assert steam_adapter.adapt_profile([]) is None and steam_adapter.adapt_profile({}) is None
    assert steam_adapter.adapt_profile({"personaname": "x"}).avatar_full is None


# ── índice de mercado ─────────────────────────────────────────────────────────

def test_market_index_fixture(payload):
    mi = steam_adapter.adapt_market_index(payload("steamwebapi/market_index"))
    assert [p.value for p in mi.history] == [100.5, 101.0] and mi.history[1].volume is None
    assert [g.item.name for g in mi.gainers] == ["Gainer A", "Sticker Slab | X"]
    assert mi.gainers[0].change_24h == 50 and mi.gainers[0].item.item_type == "glock-18"
    assert mi.losers[0].item.latest_price == 3.0 and mi.turnover_24h == 1234567.89 and mi.sold_24h == 98765


def test_market_index_formas_alternativas():
    assert steam_adapter.adapt_market_index([{"ts": "t", "value": 1}]).history[0].value == 1.0
    mi = steam_adapter.adapt_market_index({"history": {"priceindex": [{"ts": "t"}]}, "priceindex": 99})
    assert mi.history[0].value is None and mi.price_index == 99.0 and mi.gainers == ()
    # `priceindex` en la raíz puede ser la serie (lista): no es el escalar del índice.
    assert steam_adapter.adapt_market_index({"priceindex": [], "history": []}).price_index is None


def test_market_index_gainer_sin_nombre_se_descarta_no_keyerror():
    mi = steam_adapter.adapt_market_index({"history": [], "topmovers": {"gainers": [{"price": 1}, {"markethashname": "A"}]}})
    assert [g.item.name for g in mi.gainers] == ["A"] and mi.dropped_movers == 1


# ── precios por mercado e históricos ──────────────────────────────────────────

def test_price_rows(payload):
    rows = steam_adapter.adapt_price_rows(payload("steamwebapi/market_prices"), market="csfloat")
    assert {r.name: r.price for r in rows} == {"AK-47 | Redline (Field-Tested)": 18.9, "Loser A": 2.5}
    assert steam_adapter.adapt_price_rows([{"name": "x", "price": 0}, {"price": 3}], market="buff") == []


def test_history_csfloat_y_legacy(payload):
    pts = csfloat_adapter.adapt_history(payload("steamwebapi/history_csfloat"))
    assert [p["date"] for p in pts] == ["2026-09-01", "2026-09-30", "2026-10-02"]   # ordenados; el precio 0 fuera
    assert pts[-1]["volume"] == 0                                                    # quantity null → 0 (contrato int)
    legacy = history_points(payload("steamwebapi/history_legacy"), "sold", source="steamwebapi", op="history")
    assert legacy[0]["volume"] == 12 and set(legacy[0]) == {"date", "price", "volume"}
    assert buff_adapter.adapt_history([]) == []


# ── noticias, catálogo, proveedores, fx ───────────────────────────────────────

def test_news(payload):
    a, b = news_adapter.adapt_news(payload("steam_news"))
    assert a.title == "Release Notes" and a.date == 1700000000 and a.feed_name == "steam_community_announcements"
    assert b.contents == "" and b.author == ""           # noticia con contenido vacío: sigue siendo una noticia
    assert news_adapter.adapt_news({"appnews": {"newsitems": [{}]}})[0].title is None
    assert news_adapter.adapt_news({}) == [] and news_adapter.adapt_news({"appnews": {}}) == []   # contrato: sin noticias


def test_catalogo(payload):
    skins = static_catalog_adapter.adapt_catalog_source(payload("bymykel_skins"), label="skins")
    assert skins[0].wears == ("Field-Tested", "Minimal Wear") and skins[0].stattrak and skins[0].rarity_color == "d32ce6"
    assert skins[1].image == "" and skins[1].rarity_name is None
    stickers = static_catalog_adapter.adapt_catalog_source(payload("bymykel_stickers"), label="stickers")
    assert stickers[0].market_hash_name == "Sticker | Crown (Foil)" and stickers[0].wears == ()


def test_proveedores_sin_metadata_o_con_formato_raro(payload):
    providers = provider_adapter.adapt_markets(payload("steamwebapi/info_markets"))
    assert [p.id for p in providers] == ["csfloat", "buff", "skinport"]   # el `{}` sin id se descarta
    assert providers[1].logo is None and providers[2].logo == "https://skinport.com/icon.png"


def test_fx(payload):
    assert fx_adapter.adapt_rates(payload("frankfurter")).eur == 0.92
    assert fx_adapter.adapt_rates({"rates": {}}).eur is None and fx_adapter.adapt_rates({}).eur is None
    assert fx_adapter.adapt_rates({"rates": {"EUR": "lento"}}).eur is None
    assert fx_adapter.adapt_rates({"rates": {"EUR": "0.88"}}).eur is None   # string: anomalía, no tasa
