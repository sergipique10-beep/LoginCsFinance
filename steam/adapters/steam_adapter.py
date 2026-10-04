"""Adapter de steamwebapi: items, inventario, perfil, índice de mercado y lookups de
precios → modelos internos."""
from collections.abc import Mapping
from functools import partial
from typing import Any

from steam.adapters._common import mappings, require_dict, require_list
from steam.domain.models import (
    IndexPoint, MarketIndexData, PriceQuote, PriceRow, ProfileData, SteamItem, TopMover, Variant,
)
from steam.domain.validators import as_bool, as_float, as_int, as_str
from steam.errors import UNEXPECTED_FORMAT, UnexpectedPayload

SOURCE = "steamwebapi"


def adapt_item(raw: Mapping, *, op: str = "items") -> SteamItem:
    """Un item en formato plano (`/items`, `/inventory`, `/item`) o anidado
    (`/float/assets?with_items=1`: datos de mercado bajo `item`, `assetid` y `float` fuera)."""
    nested = raw.get("item")
    d: Mapping = nested if isinstance(nested, Mapping) else raw
    f = partial(as_float, source=SOURCE, op=op)
    i = partial(as_int, source=SOURCE, op=op)
    b = partial(as_bool, source=SOURCE, op=op)
    s = partial(as_str, source=SOURCE, op=op)

    float_data = raw.get("float") or d.get("float")
    variants_raw = d.get("variants") or ()
    if not isinstance(variants_raw, (list, tuple)):
        raise UnexpectedPayload(f"{UNEXPECTED_FORMAT}: {SOURCE}.{op} variants is not a list")
    prices_raw = d.get("prices") or ()
    if not isinstance(prices_raw, (list, tuple)):
        raise UnexpectedPayload(f"{UNEXPECTED_FORMAT}: {SOURCE}.{op} prices is not a list")

    return SteamItem(
        id=s(d.get("id"), field="id"),
        asset_id=s(raw.get("assetid"), field="assetid"),
        market_hash_name=s(d.get("markethashname") or d.get("market_hash_name"), field="markethashname"),
        market_name=s(d.get("marketname"), field="marketname"),
        slug=s(d.get("slug"), field="slug"),
        weapon_type=s(d.get("weapontype"), field="weapontype"),
        item_type=s(d.get("itemtype"), field="itemtype"),
        item_name=s(d.get("itemname"), field="itemname"),
        image=s(d.get("image"), field="image"),
        rarity=s(d.get("rarity"), field="rarity"),
        color=s(d.get("color"), field="color"),
        border_color=s(d.get("bordercolor"), field="bordercolor"),
        quality=s(d.get("quality"), field="quality"),
        is_stattrak=b(d.get("isstattrak"), field="isstattrak"),
        is_souvenir=b(d.get("issouvenir"), field="issouvenir"),
        is_star=b(d.get("isstar"), field="isstar"),
        exterior=s(d.get("tag5") or d.get("exterior"), field="exterior"),
        float_value=f(float_data.get("floatvalue"), field="floatvalue") if isinstance(float_data, Mapping) else None,
        float_min=f(d.get("minfloat"), field="minfloat"),
        float_max=f(d.get("maxfloat"), field="maxfloat"),
        paint_index=i(d.get("paintindex"), field="paintindex"),
        variants=tuple(
            Variant(i(v.get("paintindex"), field="variants.paintindex"), s(v.get("phase"), field="variants.phase"))
            for v in variants_raw if isinstance(v, Mapping)
        ),
        price_latest_sell=f(d.get("pricelatestsell"), field="pricelatestsell"),
        price=f(d.get("price"), field="price"),
        lowest_price=f(d.get("lowestprice"), field="lowestprice"),
        price_usd=f(d.get("priceusd"), field="priceusd"),
        price_latest=f(d.get("pricelatest"), field="pricelatest"),
        price_median=f(d.get("pricemedian"), field="pricemedian"),
        price_real=f(d.get("pricereal"), field="pricereal"),
        price_real_24h=f(d.get("pricereal24h"), field="pricereal24h"),
        price_real_7d=f(d.get("pricereal7d"), field="pricereal7d"),
        price_real_30d=f(d.get("pricereal30d"), field="pricereal30d"),
        price_safe=f(d.get("pricesafe"), field="pricesafe"),
        price_min=f(d.get("pricemin"), field="pricemin"),
        price_max=f(d.get("pricemax"), field="pricemax"),
        prices=tuple(
            PriceQuote(s(q.get("market"), field="prices.market"), f(q.get("price"), field="prices.price"),
                       i(q.get("quantity"), field="prices.quantity"))
            for q in prices_raw if isinstance(q, Mapping)
        ),
        sold_24h=i(d.get("sold24h"), field="sold24h"),
        sold_7d=i(d.get("sold7d"), field="sold7d"),
        sold_30d=i(d.get("sold30d"), field="sold30d"),
        sold_total=i(d.get("soldtotal"), field="soldtotal"),
        offer_volume=i(d.get("offervolume"), field="offervolume"),
        buy_order_volume=i(d.get("buyordervolume"), field="buyordervolume"),
        buy_order_price=f(d.get("buyorderprice"), field="buyorderprice"),
        hours_to_sold=f(d.get("hourstosold"), field="hourstosold"),
        marketable=b(d.get("marketable"), field="marketable"),
        tradable=b(d.get("tradable"), field="tradable"),
        trade_lock_days=d.get("markettradablerestriction"),
        steam_url=s(d.get("steamurl"), field="steamurl"),
    )


def adapt_items(raw: Any, *, op: str = "items") -> list[SteamItem]:
    """La lista de `/items` (búsqueda o ranking) o de `/inventory`."""
    return [adapt_item(r, op=op) for r in mappings(require_list(raw, source=SOURCE, op=op), source=SOURCE, op=op)]


def adapt_inventory(raw: Any) -> list[SteamItem]:
    return adapt_items(raw, op="inventory")


def adapt_profile(raw: Any) -> ProfileData | None:
    """`/profile`: steamwebapi devuelve una lista con el perfil (o vacía) o el objeto.
    None = 200 sin perfil; antes se servían campos vacíos y se cacheaban 23 h (CAL-14)."""
    data = raw
    if isinstance(raw, list):
        if not raw:
            return None
        data = raw[0]
    if not data:
        return None
    d = require_dict(data, source=SOURCE, op="profile")
    s = partial(as_str, source=SOURCE, op="profile")
    return ProfileData(
        persona_name=s(d.get("personaname"), field="personaname"),
        avatar_full=s(d.get("avatarfull"), field="avatarfull"),
        avatar_medium=s(d.get("avatarmedium"), field="avatarmedium"),
        profile_url=s(d.get("profileurl"), field="profileurl"),
        persona_state=as_int(d.get("personastate"), field="personastate", source=SOURCE, op="profile"),
    )


def _top_movers(raw: Any) -> tuple[tuple[TopMover, ...], int]:
    """(movers, descartados): un gainer/loser sin `markethashname` no es un item
    (antes era un `KeyError` → 500 en /market/index, CAL-14)."""
    if raw is None:
        return (), 0
    movers: list[TopMover] = []
    dropped = 0
    for m in mappings(require_list(raw, source=SOURCE, op="market_index.topmovers"),
                      source=SOURCE, op="market_index.topmovers"):
        item = adapt_item(m, op="market_index.topmovers")
        if not item.market_hash_name:
            dropped += 1
            continue
        movers.append(TopMover(item, as_float(m.get("change24h"), field="change24h", source=SOURCE,
                                              op="market_index.topmovers")))
    return tuple(movers), dropped


def adapt_market_index(raw: Any) -> MarketIndexData:
    """`/market-index/cs2`. Admite la forma antigua (una lista de puntos) y la actual
    (objeto con `history` lista, o `history.priceindex` lista, más `topmovers`)."""
    op = "market_index"
    f = partial(as_float, source=SOURCE, op=op)
    if isinstance(raw, list):
        points_raw: Any = raw
        d: Mapping = {}
    else:
        d = require_dict(raw, source=SOURCE, op=op)
        history = d.get("history", [])
        if isinstance(history, Mapping):
            points_raw = history.get("priceindex", [])
        else:
            points_raw = history
    points = tuple(
        IndexPoint(ts=as_str(p.get("ts"), field="ts", source=SOURCE, op=op) or "",
                   value=f(p.get("value"), field="value"), change=f(p.get("change"), field="change"),
                   volume=as_int(p.get("volume"), field="volume", source=SOURCE, op=op))
        for p in mappings(require_list(points_raw, source=SOURCE, op=f"{op}.history"), source=SOURCE, op=op)
    )
    topmovers = d.get("topmovers")
    topmovers = {} if topmovers is None else require_dict(topmovers, source=SOURCE, op=f"{op}.topmovers")
    gainers, dropped_g = _top_movers(topmovers.get("gainers"))
    losers, dropped_l = _top_movers(topmovers.get("losers"))
    return MarketIndexData(
        history=points, gainers=gainers, losers=losers,
        turnover_24h=f(d.get("turnover24h"), field="turnover24h"),
        sold_24h=as_int(d.get("sold24h"), field="sold24h", source=SOURCE, op=op),
        price_index=f(d.get("priceindex"), field="priceindex"),
        real_price_index=f(d.get("realpriceindex"), field="realpriceindex"),
        buy_order_price_index=f(d.get("buyorderpriceindex"), field="buyorderpriceindex"),
        dropped_movers=dropped_g + dropped_l,
    )


def adapt_price_rows(raw: Any, *, market: str) -> list[PriceRow]:
    """`/market/{market}/prices` entero: filas con nombre y precio > 0; el resto no es
    un precio (antes `lookup.get(name) or None` las tiraba igual)."""
    op = f"{market}.prices"
    rows: list[PriceRow] = []
    for r in mappings(require_list(raw, source=SOURCE, op=op), source=SOURCE, op=op):
        name = as_str(r.get("market_hash_name") or r.get("markethashname") or r.get("name"),
                      field="market_hash_name", source=SOURCE, op=op)
        price = as_float(r.get("price") if r.get("price") is not None else r.get("value"),
                         field="price", source=SOURCE, op=op)
        if name and price and price > 0:
            rows.append(PriceRow(name, price))
    return rows
