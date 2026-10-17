"""COST-05 · Rankings, índice y cierre diario del worker (worker/jobs/rankings.py, worker/adapters/own_card.py)."""
import datetime as dt
import json
import subprocess
import sys
from types import SimpleNamespace

import fakeredis
import pytest

from steam.domain.rules import MIN_RANKING_PRICE
from steam.mappers.row_mapper import _to_row
from worker import settings
from worker.adapters import own_card
from worker.adapters import skinport as skinport_adapter
from worker.jobs import market_sync as ms
from worker.jobs import rankings as rk
from worker.models import CatalogItem, Quote
from worker.pricing import index as ix
from worker.pricing import reference

NOW = dt.datetime(2026, 10, 17, 18, 6, tzinfo=dt.UTC)


def ref(name, price, vol=10, steam_age_h=None):
    quotes = [Quote(NOW, name, "skinport", price, None, 7), Quote(NOW, name, "skinport_sales", price, vol)]
    if steam_age_h is not None:
        quotes.append(Quote(NOW - dt.timedelta(hours=steam_age_h), name, "steam", price * 1.1, vol + 1))
    return reference.compute(name, quotes, NOW)


def cat(name, weapon="AK-47", rarity="Covert", color="#eb4b4b"):
    return CatalogItem(name, weapon, "Rifles", rarity, color, f"https://img/{name}.png")


# ---------------------------------------------------------------- tarjeta

def test_tarjeta_misma_forma_que_la_fila_de_hoy():
    r = ref("StatTrak™ AK-47 | Redline (Field-Tested)", 25.0, vol=12)
    s = ix.SalesStats(volume_24h=12, volume_7d=80, volume_30d=300, median_24h=24.0, median_7d=20.0, median_30d=30.0)
    c = own_card.card(r, cat(r.market_hash_name), s, own_card.Closes(d1=24.0))
    row = _to_row(c, 3)
    assert row["name"] == r.market_hash_name and row["rank"] == 3
    assert (row["weapon_type"], row["item_name"], row["exterior"]) == ("Rifle", "Redline", "Field-Tested")
    assert (row["is_stat_trak"], row["quality"], row["rarity_color"]) == (True, "StatTrak™", "eb4b4b")
    assert row["price_latest"] == 25.0 and row["sold_24h"] == 12 and row["sold_30d"] == 300
    assert row["turnover"] == 300.0 and row["offer_volume"] == 7
    assert row["price_delta_24h"] == pytest.approx(4.17, abs=0.01)          # serie propia: (25-24)/24
    assert row["price_delta_7d"] == pytest.approx(20.0)                      # proxy Skinport: (24-20)/20
    assert row["price_delta_30d"] == pytest.approx(-20.0)
    assert "liquidity_breakdown" not in row and "Redline" in row["steam_url"]


def test_tarjeta_sin_catalogo_ni_ventas_no_rompe():
    c = own_card.card(ref("★ Karambit | Fade (Factory New)", 1500.0), None, None, own_card.Closes())
    assert (c["weaponType"], c["image"], c["rarity"], c["isStar"]) == (None, "", "Base Grade", True)
    assert (c["priceDelta24h"], c["priceDelta7d"], c["sold7d"]) == (None, None, 0)
    assert own_card.split_name("Sticker | Crown (Foil)") == ("Crown (Foil)", None)
    assert own_card.split_name("Operation Bravo Case") == (None, None)


def test_delta_implausible_es_none():
    assert own_card.pct(17.57, 0.22) is None and own_card.pct(10.0, 8.0) == 25.0


# ---------------------------------------------------------------- rankings puros

def cards(n, price=20.0, vol=10, weapon="AK-47", delta7=None, name_fmt="AK-47 | Skin{i} (Field-Tested)"):
    out = []
    for i in range(n):
        r = ref(name_fmt.format(i=i), price + i, vol)
        c = own_card.card(r, cat(r.market_hash_name, weapon), None, own_card.Closes())
        c["priceDelta7d"] = delta7(i) if delta7 else None
        out.append(c)
    return out


def test_trending_filtra_precio_ventas_y_slabs_y_ordena_por_turnover():
    buenos = cards(3)
    barato = cards(1, price=MIN_RANKING_PRICE - 1, name_fmt="Glock-18 | Barata{i} (Field-Tested)")
    sin_ventas = cards(1, vol=0, name_fmt="AWP | Parada{i} (Field-Tested)")
    slab = cards(1, name_fmt="Sticker Slab | Crown{i}")
    slab[0]["itemType"] = "sticker slab"
    out = rk.compute_trending(barato + slab + buenos + sin_ventas)
    assert [c["name"] for c in out] == [c["name"] for c in sorted(buenos, key=lambda c: -c["priceLatest"])]


def test_movers_hot_cold_por_delta_7d():
    out = rk.compute_movers(cards(30, vol=6, delta7=lambda i: float(i - 15)))
    assert len(out["hot"]) == 10 and len(out["cold"]) == 10
    hot = [c["priceDelta7d"] for c in out["hot"]]
    cold = [c["priceDelta7d"] for c in out["cold"]]
    # Los 20 candidatos son los de más turnover (i = 10..29 → deltas -5..14); hot baja, cold sube.
    assert hot == sorted(hot, reverse=True) and hot[0] == 14.0 and cold == sorted(cold) and cold[0] == -5.0
    assert not rk.compute_movers(cards(5, vol=4))["hot"]                     # MIN_SOLD_MOVERS = 5


def test_steam_fresco_solo_si_es_reciente():
    refs = [ref("A", 10.0, steam_age_h=1), ref("B", 10.0, steam_age_h=3), ref("C", 10.0)]
    assert set(rk.steam_fresh(refs, NOW)) == {"A"} and rk.steam_fresh(refs, NOW)["A"] == (11.0, 11)


def test_sales_stats_del_adapter():
    st = skinport_adapter.sales_stats([{"market_hash_name": "AK", "currency": "USD",
                                        "last_24_hours": {"median": 10, "volume": 2},
                                        "last_30_days": {"avg": 9.5, "median": 9, "volume": 60}},
                                       {"market_hash_name": "EUR", "currency": "EUR"}])
    assert list(st) == ["AK"] and ix.SalesStats(**st["AK"]).turnover_30d == 570.0


# ---------------------------------------------------------------- jobs con fakes

@pytest.fixture
def fake(monkeypatch):
    db = SimpleNamespace(refs=[], catalog={}, closes={}, volume={}, cap=None, tracked=[], upserts=[], replaced=[],
                         purged=[], catalog_runs=0, ok=[], fail=[])

    async def compute_references(state, now):
        return db.refs

    async def run_catalog(state):
        db.catalog_runs += 1
        db.catalog = {"AK-47 | Skin0 (Field-Tested)": cat("AK-47 | Skin0 (Field-Tested)")}
        return {"ok": True}

    async def catalog_all(pool):
        return db.catalog

    async def daily_closes(pool, days):
        return {n: {d: v for d, v in c.items() if d in days} for n, c in db.closes.items()}

    async def daily_volume(pool, day):
        return db.volume

    async def upsert(sb, table, rows, on_conflict, batch=500):
        db.upserts.append((table, rows, on_conflict))
        return len(rows)

    async def replace_all(sb, table, rows, key="name"):
        db.replaced.append((table, rows))
        return len(rows)

    async def delete_older(sb, table, column, before):
        db.purged.append((table, column))

    async def last_cap_point(sb, table="market_cap_history"):
        return db.cap

    async def select_all(sb, table, columns, page=1000, **eq):
        return [{"market_hash_name": n} for n in db.tracked]

    async def source_ok(pool, s):
        db.ok.append(s)

    async def source_fail(pool, s, e):
        db.fail.append((s, e))

    async def push(*a, **k):
        return True
    monkeypatch.setattr(ms, "compute_references", compute_references)
    monkeypatch.setattr(ms, "run_catalog", run_catalog)
    monkeypatch.setattr(ms, "now_utc", lambda: NOW)
    monkeypatch.setattr(ms.health, "push", push)
    for n, f in dict(catalog_all=catalog_all, daily_closes=daily_closes, daily_volume=daily_volume,
                     source_ok=source_ok, source_fail=source_fail).items():
        monkeypatch.setattr(rk.market, n, f)
    for n, f in dict(upsert=upsert, replace_all=replace_all, delete_older=delete_older,
                     last_cap_point=last_cap_point, select_all=select_all).items():
        monkeypatch.setattr(rk.sb_store, n, f)
    return db


def state():
    return SimpleNamespace(redis=fakeredis.aioredis.FakeRedis(), pg=object(), supabase=object(), http=None)


async def test_compute_rankings_escribe_sombra_y_trae_catalogo_si_falta(fake):
    fake.refs = [ref(f"AK-47 | Skin{i} (Field-Tested)", 20.0 + i, vol=6) for i in range(3)]
    res = await rk.run_compute_rankings(state())
    assert res["ok"] and res["trending"] == 3 and fake.catalog_runs == 1
    table, rows, key = fake.upserts[0]
    assert table == settings.MARKET_TRENDING_TABLE == "market_trending_own" and key == "name"
    assert rows[0]["name"] == "AK-47 | Skin2 (Field-Tested)" and rows[0]["seen_at"] == NOW.isoformat()
    by_name = {r["name"]: r for r in rows}        # el catálogo recién traído solo trae Skin0
    assert by_name["AK-47 | Skin0 (Field-Tested)"]["weapon_type"] == "Rifle" and rows[0]["weapon_type"] is None
    assert fake.purged == [("market_trending_own", "seen_at")]
    assert fake.replaced[0][0] == "market_movers_own" and {r["bucket"] for r in fake.replaced[0][1]} == {"hot", "cold"}
    assert fake.ok == ["compute_rankings"]


async def test_compute_rankings_sin_elegibles_conserva_movers(fake):
    fake.refs = [ref("AK-47 | Skin0 (Field-Tested)", 5.0)]
    res = await rk.run_compute_rankings(state())
    assert res["ok"] and res["trending"] == 0 and not fake.upserts and not fake.replaced


async def test_compute_index_arranca_en_i0_y_encadena(fake):
    st = state()
    names = [f"n{i}" for i in range(150)]
    stats = {n: {"volume_24h": 1, "volume_30d": 10, "avg_30d": 10.0, "median_24h": 10.0} for n in names}
    await st.redis.hset(ms.SALES_KEY, mapping={n: json.dumps(v) for n, v in stats.items()})
    fake.refs = [ref(n, 10.0, vol=1) for n in names]
    assert not (await rk.run_compute_index(st))["ok"]                       # sin I_0 no hay índice
    fake.cap = {"ts": "2026-10-17T15:00:00+00:00", "priceindex": 2859545.83, "realpriceindex": 3545814.37}
    res = await rk.run_compute_index(st)
    assert res["ok"] and res["priceindex"] == 2859545.83 and res["chained"] == 0 and res["basket"] == 150
    point = fake.upserts[-1][1][0]
    assert fake.upserts[-1][0] == "market_cap_history_own" and point["ts"] == "2026-10-17T18:00:00+00:00"
    assert point["realpriceindex"] == 3545814.37 and point["buyorderpriceindex"] is None
    assert (point["turnover24h"], point["sold24h"]) == (1500.0, 150)
    fake.refs = [ref(n, 11.0, vol=1) for n in names]
    res = await rk.run_compute_index(st)
    assert res["priceindex"] == pytest.approx(2859545.83 * 1.1, abs=0.01) and res["chained"] == 150


async def test_compute_index_sin_ventas_falla_visible(fake):
    assert "skinport_sales" in (await rk.run_compute_index(state()))["error"]


async def test_daily_close_escribe_ayer_de_las_tracked(fake):
    ayer = (NOW - dt.timedelta(days=1)).date()
    fake.closes = {"A": {ayer: 12.3456}, "B": {ayer: 5.0}, "C": {ayer - dt.timedelta(days=1): 9.0}}
    fake.volume = {"A": 7}
    fake.tracked = ["A", "C", "Z"]
    res = await rk.run_daily_close(state())
    assert res == {"ok": True, "date": ayer.isoformat(), "tracked": 3, "rows": 1}
    table, rows, key = fake.upserts[0]
    assert table == "precios_historicos_own" and key == "market_hash_name,date"
    assert rows == [{"market_hash_name": "A", "date": ayer.isoformat(), "price": 12.3456, "volume": 7, "source": "own"}]


def test_el_dominio_que_viaja_en_la_imagen_no_arrastra_el_backend():
    """El Dockerfile del worker copia steam/domain, row_mapper y utils/urls: importarlos no puede tirar de
    steam.errors, settings ni httpx (no están en la imagen)."""
    code = ("import sys; import steam.domain.rules, steam.domain.catalog, steam.mappers.row_mapper, worker.adapters.own_card;"
            "print(sorted(m for m in sys.modules if m in ('settings', 'httpx', 'supabase') or m.startswith('steam.errors')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)  # noqa: S603
    assert out.stdout.strip() == "[]"
