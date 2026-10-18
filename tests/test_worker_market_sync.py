"""COST-03 · Fuentes, jobs de market-sync y almacenamiento (worker/sources, worker/jobs/market_sync.py)."""
import asyncio
import datetime as dt
import os
import pathlib
from types import SimpleNamespace

import fakeredis
import httpx
import pytest
import stamina

from worker import settings
from worker.jobs import market_sync as ms
from worker.models import CatalogItem, Quote
from worker.net.breaker import Breakers
from worker.net.gate import Gate
from worker.pricing.reference import Reference
from worker.sources import _fetch, catalog, csgotm, csgotrader, fx, skinport
from worker.store import market
from worker.store import supabase as sb_store

NOW = dt.datetime(2026, 10, 13, 12, tzinfo=dt.UTC)
NOJITTER = {h: settings.HostLimit(rate=1000, per_s=1, burst=1000, jitter=(0, 0)) for h in settings.DEFAULT_LIMITS}


@pytest.fixture(autouse=True)
def _fast():
    with stamina.set_testing(True, attempts=3):
        yield


def make_state(handler, pg=None, supabase=None):
    server = fakeredis.FakeServer()
    r = fakeredis.aioredis.FakeRedis(server=server)
    return SimpleNamespace(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), redis=r,
                           gate=Gate(r, NOJITTER), breakers=Breakers(fakeredis.FakeRedis(server=server), NOJITTER),
                           pg=pg, supabase=supabase)


class FakePG:
    """Sustituye a store.market: guarda lo que se escribiría."""

    def __init__(self, latest=None):
        self.quotes, self.refs, self.catalog, self.ok, self.fail = [], [], [], [], []
        self.latest = latest or {}


@pytest.fixture
def fake_market(monkeypatch):
    db = FakePG()

    async def insert_quotes(pool, quotes):
        db.quotes += quotes
        return len(quotes)

    async def insert_refs(pool, ts, refs):
        db.refs += refs

    async def upsert_catalog(pool, items):
        db.catalog += items

    async def latest_quotes(pool, since):
        return db.latest

    async def source_ok(pool, s):
        db.ok.append(s)

    async def source_fail(pool, s, e):
        db.fail.append((s, e))
    for n, f in dict(insert_quotes=insert_quotes, insert_refs=insert_refs, upsert_catalog=upsert_catalog,
                     latest_quotes=latest_quotes, source_ok=source_ok, source_fail=source_fail).items():
        monkeypatch.setattr(ms.market, n, f)

    async def push(*a, **k):
        return True
    monkeypatch.setattr(ms.health, "push", push)
    monkeypatch.setattr(ms, "now_utc", lambda: NOW)
    return db


def router(routes):
    def handler(req):
        for frag, resp in routes.items():
            if frag in str(req.url):
                return resp(req) if callable(resp) else resp
        return httpx.Response(404)
    return handler


async def test_skinport_items_y_sales(fake_market):
    seen = []

    def items(req):
        seen.append(req.headers.get("accept-encoding"))
        return httpx.Response(200, json=[{"market_hash_name": "AK", "currency": "USD", "min_price": 9, "quantity": 3}])
    st = make_state(router({"/v1/items": items, "/v1/sales/history": httpx.Response(200, json=[
        {"market_hash_name": "AK", "currency": "USD", "last_24_hours": {"median": 10, "volume": 2}}])}))
    assert (await ms.run_skinport_items(st))["quotes"] == 1
    assert (await ms.run_skinport_sales(st))["quotes"] == 1
    assert seen == ["br"] and fake_market.ok == ["skinport_items", "skinport_sales"]


async def test_csgotm(fake_market):
    st = make_state(router({"prices/USD.json": httpx.Response(200, json={"currency": "USD", "items": [
        {"market_hash_name": "AK", "price": "9.5", "volume": "4"}]})}))
    assert (await ms.run_csgotm(st))["quotes"] == 1


async def test_csgotrader_sin_moneda_no_descarga(fake_market, monkeypatch):
    monkeypatch.setattr(settings, "BUFF_CURRENCY", "")
    st = make_state(router({}))
    res = await ms.run_csgotrader(st)
    assert not res["ok"] and "BUFF_CURRENCY" in res["error"] and fake_market.fail[0][0] == "csgotrader_buff"


async def test_csgotrader_etag_y_cny(fake_market, monkeypatch):
    monkeypatch.setattr(settings, "BUFF_CURRENCY", "CNY")
    calls = []

    def buff(req):
        calls.append(req.headers.get("if-none-match"))
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json={"AK": {"starting_at": {"price": 70}}}, headers={"ETag": '"v1"'})
    st = make_state(router({"buff163.json": buff, "frankfurter": httpx.Response(200, json={"rates": {"CNY": 7.0}})}))
    assert (await ms.run_fx(st))["usd_per_cny"] == round(1 / 7, 6)
    assert (await ms.run_csgotrader(st))["quotes"] == 1
    assert fake_market.quotes[0].price_usd == pytest.approx(10.0, abs=1e-3)
    assert (await ms.run_csgotrader(st))["not_modified"] is True
    assert calls == [None, '"v1"']


async def test_fx_implausible_falla(fake_market):
    st = make_state(router({"frankfurter": httpx.Response(200, json={"rates": {"CNY": 700}})}))
    assert not (await ms.run_fx(st))["ok"]


async def test_catalogo(fake_market):
    st = make_state(router({"skins_not_grouped.json": httpx.Response(200, json=[{"market_hash_name": "AK"}]),
                            ".json": httpx.Response(200, json=[])}))
    assert (await ms.run_catalog(st))["items"] == 1 and fake_market.catalog[0].market_hash_name == "AK"


async def test_fallo_de_red_queda_en_source_health(fake_market):
    st = make_state(router({"/v1/items": httpx.Response(503)}))
    res = await ms.run_skinport_items(st)
    assert not res["ok"] and fake_market.fail and "503" in fake_market.fail[0][1]


async def test_compute_reference_solo_sube_lo_que_cambia(fake_market, monkeypatch):
    sent = []

    async def upsert(sb, table, rows, on_conflict, batch=500):
        sent.append((table, [r["market_hash_name"] for r in rows]))
        return len(rows)
    monkeypatch.setattr(ms.sb_store, "upsert", upsert)
    fake_market.latest = {"AK": [Quote(NOW, "AK", "skinport", 10.0, None, 3),
                                 Quote(NOW, "AK", "marketcsgo", 10.4, None, 9)],
                          "M4": [Quote(NOW, "M4", "skinport", 5.0, None, 1)]}
    st = make_state(router({}), supabase="sb")
    first = await ms.run_compute_reference(st)
    assert first["changed"] == 2 and first["confidence_ge2"] == 1 and sent[0][0] == "market_current"
    # Mismo precio, cotizaciones más nuevas: el hash ignora las marcas de tiempo → nada que subir.
    later = NOW + dt.timedelta(minutes=15)
    fake_market.latest = {k: [Quote(later, *[getattr(x, f) for f in ("market_hash_name", "source", "price_usd",
                                                                     "volume", "listings")]) for x in v]
                          for k, v in fake_market.latest.items()}
    second = await ms.run_compute_reference(st)
    assert second["changed"] == 0 and len(sent) == 1
    fake_market.latest["M4"] = [Quote(later, "M4", "skinport", 6.0, None, 1)]
    third = await ms.run_compute_reference(st)
    assert third["changed"] == 1 and sent[-1][1] == ["M4"] and third["changed_pct"] == 50.0
    # market_ref solo recibe las referencias que cambian: 2 + 0 + 1, no 3 por tick.
    assert [r.market_hash_name for r in fake_market.refs] == ["AK", "M4", "M4"]


def test_tareas_registradas_con_su_cron():
    crons = {t.task_name: t.labels["schedule"][0]["cron"] for t in
             (ms.skinport_items, ms.skinport_sales, ms.csgotm_prices, ms.csgotrader_buff, ms.fx_daily,
              ms.catalog_weekly, ms.compute_reference)}
    assert crons["skinport_items"] == "*/15 * * * *" and crons["compute_reference"] == "2,17,32,47 * * * *"


async def test_las_tareas_delegan(monkeypatch):
    called = []
    for name in ("run_skinport_items", "run_skinport_sales", "run_csgotm", "run_csgotrader", "run_fx",
                 "run_catalog", "run_compute_reference"):
        async def f(state, _n=name):
            called.append(_n)
            return {}
        monkeypatch.setattr(ms, name, f)
    ctx = SimpleNamespace(state=None)
    for t in (ms.skinport_items, ms.skinport_sales, ms.csgotm_prices, ms.csgotrader_buff, ms.fx_daily,
              ms.catalog_weekly, ms.compute_reference):
        await t.original_func(ctx)
    assert len(called) == 7


async def test_fuentes_sueltas():
    st = make_state(router({"api.skinport.com": httpx.Response(200, json=[]),
                            "market.csgo.com": httpx.Response(200, json={}),
                            "frankfurter": httpx.Response(200, json={}),
                            "githubusercontent": httpx.Response(200, json={"no": "lista"})}))
    assert await skinport.items(st) == [] and await skinport.sales_history(st) == []
    assert await csgotm.prices(st) == {} and await fx.usd_cny(st) == {}
    assert await catalog.all_files(st) == []
    assert csgotrader.URL.endswith("buff163.json")
    assert await _fetch.get_json_etag(st, "https://market.csgo.com/x", None) == ({}, None)


async def test_supabase_upsert_por_lotes():
    calls = []

    class T:
        def upsert(self, rows, on_conflict):
            calls.append((len(rows), on_conflict))
            return self

        def execute(self):
            return None
    assert await sb_store.upsert(SimpleNamespace(table=lambda n: T()), "t", [{}] * 1200, "k") == 1200
    assert calls == [(500, "k"), (500, "k"), (200, "k")]


# ---------------------------------------------------------------- integración con Timescale
DSN = os.environ.get("WORKER_PG_TEST_DSN")


@pytest.mark.skipif(not DSN, reason="sin WORKER_PG_TEST_DSN (Timescale con deploy/vps/schema.sql)")
def test_store_market_contra_timescale():
    from worker.store import pg

    async def m():
        pool = await pg.create_pool(DSN)
        try:
            async with pool.acquire() as c:
                await c.execute("truncate market_quotes, market_latest, market_ref, catalog_items, source_health")
            old = NOW - dt.timedelta(hours=1)
            await market.insert_quotes(pool, [Quote(old, "AK", "skinport", 9.0, None, 2)])
            await market.insert_quotes(pool, [Quote(NOW, "AK", "skinport", 10.0, None, 3),
                                              Quote(NOW, "AK", "marketcsgo", 10.5, 4, None)])
            latest = await market.latest_quotes(pool, NOW - dt.timedelta(days=1))
            assert sorted((q.source, q.price_usd) for q in latest["AK"]) == [("marketcsgo", 10.5), ("skinport", 10.0)]
            # Misma cotización más tarde: market_latest avanza seen_at, la serie no crece.
            later = NOW + dt.timedelta(minutes=5)
            await market.insert_quotes(pool, [Quote(later, "AK", "skinport", 10.0, None, 3)])
            async with pool.acquire() as c:
                assert await c.fetchval("select count(*) from market_quotes") == 3
                assert await c.fetchval("select seen_at from market_latest where source = 'skinport'") == later
                assert await c.fetchval("select ts from market_latest where source = 'skinport'") == NOW
            await market.insert_refs(pool, NOW, [Reference("AK", 10.2, 2, NOW, 2, 4)])
            await market.upsert_catalog(pool, [CatalogItem("AK", "AK-47", "Rifles", "Classified", "#fff", None)])
            await market.upsert_catalog(pool, [CatalogItem("AK", "AK-47", "Rifles", "Covert", "#f00", None)])
            await market.source_ok(pool, "skinport_items")
            await market.source_fail(pool, "skinport_items", "x")
            await market.source_fail(pool, "skinport_items", "y")
            async with pool.acquire() as c:
                assert await c.fetchval("select rarity from catalog_items") == "Covert"
                assert await c.fetchval("select consecutive_failures from source_health") == 2
                assert await c.fetchval("select count(*) from market_ref") == 1
        finally:
            await pool.close()
    asyncio.run(m())


def test_ddl_market_current_existe():
    sql = (pathlib.Path(__file__).parent.parent / "docs/sql/market_current.sql").read_text(encoding="utf-8")
    assert "gin_trgm_ops" in sql and "turnover_24h desc" in sql and "enable row level security" in sql
