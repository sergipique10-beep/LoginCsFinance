"""COST-04 · Steam top-N: priceoverview por el gate, rotación en Valkey, presupuesto y cooldown."""
import datetime as dt
from types import SimpleNamespace

import fakeredis
import httpx
import pytest
import stamina

from worker import settings
from worker.adapters import steam_market as adapter
from worker.jobs import steam_topn as st
from worker.net.breaker import Breakers
from worker.net.gate import Gate
from worker.sources import steam_market

NOW = dt.datetime(2026, 10, 16, 20, tzinfo=dt.UTC)
NOJITTER = {h: settings.HostLimit(rate=1000, per_s=1, burst=1000, jitter=(0, 0), steam=lim.steam)
            for h, lim in settings.DEFAULT_LIMITS.items()}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(st, "now_utc", lambda: NOW)
    with stamina.set_testing(True, attempts=3):
        yield


def make_state(handler):
    server = fakeredis.FakeServer()
    r = fakeredis.aioredis.FakeRedis(server=server)
    return SimpleNamespace(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), redis=r,
                           gate=Gate(r, NOJITTER), breakers=Breakers(fakeredis.FakeRedis(server=server), NOJITTER),
                           pg=object(), supabase=object())


@pytest.fixture
def fake_store(monkeypatch):
    db = SimpleNamespace(quotes=[], ok=[], fail=[], alerts=["AK-47 | Redline (Field-Tested)"],
                         inv=["AWP | Asiimov (Field-Tested)", "AK-47 | Redline (Field-Tested)",
                              "Glock-18 | Fade (Factory New)"])

    async def insert_quotes(pool, quotes):
        db.quotes += quotes
        return len(quotes)

    async def source_ok(pool, s):
        db.ok.append(s)

    async def source_fail(pool, s, e):
        db.fail.append((s, e))

    async def recent_inventory_names(pool, min_price):
        return db.inv

    async def active_alert_names(sb):
        return db.alerts

    async def push(*a, **k):
        return True
    for n, f in dict(insert_quotes=insert_quotes, source_ok=source_ok, source_fail=source_fail,
                     recent_inventory_names=recent_inventory_names).items():
        monkeypatch.setattr(st.market, n, f)
    monkeypatch.setattr(st.sb_store, "active_alert_names", active_alert_names)
    import worker.jobs.market_sync as ms
    monkeypatch.setattr(ms.health, "push", push)
    return db


# ---------------------------------------------------------------- adapter

def test_parse_money():
    assert adapter.parse_money("$1,234.56") == 1234.56
    assert adapter.parse_money("$0.03") == 0.03
    assert adapter.parse_money("1,234") == 1234.0
    assert adapter.parse_money(None) is None and adapter.parse_money("n/a") is None


def test_adapt_priceoverview():
    q = adapter.adapt_priceoverview({"success": True, "lowest_price": "$12.34", "median_price": "$12.00", "volume": "1,234"},
                                    "X", NOW)
    assert (q.source, q.price_usd, q.volume, q.ts) == ("steam", 12.34, 1234, NOW)
    assert adapter.adapt_priceoverview({"success": True, "volume": "5"}, "X", NOW) is None   # sin precio
    assert adapter.adapt_priceoverview({"success": False}, "X", NOW) is None
    assert adapter.adapt_priceoverview(None, "X", NOW) is None
    assert adapter.adapt_priceoverview({"success": True, "lowest_price": "$1.00"}, "X", NOW).volume is None


# ---------------------------------------------------------------- top-N

def test_merge_names_prioridad_dedupe_y_tope():
    assert st.merge_names(["a", "b"], ["b", "c"], ["d", "a", ""], cap=3) == ["a", "b", "c"]


async def test_topn_job_escribe_la_lista_en_orden(fake_store):
    """Alertas primero, inventarios después, sin repetir; ningún top por turnover (N pequeño)."""
    state = make_state(lambda req: httpx.Response(500))
    res = await st.run_topn(state)
    assert res == {"ok": True, "names": 3}
    names = [n.decode() for n in await state.redis.lrange(st.TOPN_KEY, 0, -1)]
    assert names == ["AK-47 | Redline (Field-Tested)", "AWP | Asiimov (Field-Tested)",
                     "Glock-18 | Fade (Factory New)"]
    assert fake_store.ok == ["steam_topn"]
    assert not hasattr(st.sb_store, "top_turnover_names")


async def test_topn_respeta_el_tope(fake_store, monkeypatch):
    monkeypatch.setattr(settings, "STEAM_TOPN", 2)
    state = make_state(lambda req: httpx.Response(500))
    assert (await st.run_topn(state))["names"] == 2


# ---------------------------------------------------------------- priceoverview

def _steam(prices):
    seen = []

    def handler(req):
        name = req.url.params["market_hash_name"]
        seen.append(name)
        assert req.url.params["currency"] == "1" and req.url.params["appid"] == "730"
        p = prices.get(name)
        if p == 429:
            return httpx.Response(429)
        if p is None:
            return httpx.Response(500, json={"success": False})
        return httpx.Response(200, json={"success": True, "lowest_price": f"${p}", "volume": "10"})
    return handler, seen


async def test_tick_rota_la_lista_y_guarda_cotizaciones(fake_store, monkeypatch):
    monkeypatch.setattr(settings, "STEAM_PRICEOVERVIEW_PER_TICK", 2)
    handler, seen = _steam({"a": "1.00", "b": "2.50", "c": "3.00"})
    state = make_state(handler)
    await state.redis.rpush(st.TOPN_KEY, "a", "b", "c")
    res = await st.run_priceoverview(state)
    assert res == {"ok": True, "quotes": 2, "misses": 0, "spent_today": 2}
    assert seen == ["a", "b"]
    assert [q.market_hash_name for q in fake_store.quotes] == ["a", "b"]
    assert fake_store.quotes[1].price_usd == 2.5 and fake_store.quotes[1].volume == 10
    # La lista ha rotado: el siguiente tick empieza por "c".
    assert [n.decode() for n in await state.redis.lrange(st.TOPN_KEY, 0, -1)] == ["c", "a", "b"]
    res2 = await st.run_priceoverview(state)
    assert seen[2:] == ["c", "a"] and res2["spent_today"] == 4


async def test_tick_con_lista_vacia(fake_store):
    state = make_state(lambda req: httpx.Response(500))
    res = await st.run_priceoverview(state)
    assert res == {"ok": True, "quotes": 0, "misses": 0, "spent_today": 0}


async def test_tick_para_al_agotar_el_presupuesto(fake_store, monkeypatch):
    monkeypatch.setattr(settings, "STEAM_BUDGET_PRICEOVERVIEW", 1)
    handler, seen = _steam({"a": "1.00", "b": "2.00"})
    state = make_state(handler)
    await state.redis.rpush(st.TOPN_KEY, "a", "b")
    res = await st.run_priceoverview(state)
    assert res["quotes"] == 1 and res["stop"] == "presupuesto agotado" and seen == ["a"]


async def test_nombre_desconocido_cuenta_como_miss(fake_store):
    handler, seen = _steam({"a": "1.00", "b": None})
    state = make_state(handler)
    await state.redis.rpush(st.TOPN_KEY, "b", "a")
    res = await st.run_priceoverview(state)
    assert res["quotes"] == 1 and res["misses"] == 1 and res["ok"] is True
    assert seen.count("b") == settings.RETRY_ATTEMPTS   # el 500 se reintenta como cualquier 5xx
    assert await state.redis.ttl(st.MISS_KEY.format(name="b")) > 0
    # Siguiente tick: "b" se salta sin gastar presupuesto ni tocar Steam.
    res2 = await st.run_priceoverview(state)
    assert res2["quotes"] == 1 and res2["misses"] == 0 and seen.count("b") == settings.RETRY_ATTEMPTS
    assert res2["spent_today"] == 3


async def test_429_abre_cooldown_y_el_tick_falla(fake_store, caplog):
    handler, seen = _steam({"a": 429, "b": "1.00"})
    state = make_state(handler)
    await state.redis.rpush(st.TOPN_KEY, "a", "b")
    res = await st.run_priceoverview(state)
    assert res["ok"] is False and "RateLimited" in res["error"]
    assert "429 en la petición 1 del tick, nombre='a'" in caplog.text
    assert seen == ["a"]                                   # no se sigue pidiendo
    assert await state.gate.cooldown_left("steamcommunity.com") > 0
    assert await state.gate.cooldown_left("steamcommunity.com/inventory") == 0   # ámbito aparte
    assert fake_store.fail[0][0] == "steam_priceoverview"
    # Siguiente tick: el cooldown lo para antes de salir a Steam, y antes de rotar o gastar.
    spent_before = await state.gate.spent(st.BUDGET_KIND)
    res2 = await st.run_priceoverview(state)
    assert res2["ok"] is False and "cooldown" in res2["error"] and seen == ["a"]
    assert [n.decode() for n in await state.redis.lrange(st.TOPN_KEY, 0, -1)] == ["b", "a"]   # sin LMOVE
    assert await state.gate.spent(st.BUDGET_KIND) == spent_before                               # sin try_spend


def test_cadencia_y_ritmo_por_defecto_caben_en_media_cuota():
    """2 nombres cada 30 min = 96/día < 120 de presupuesto (cuota mínima vista: ~100–150 por cubo y ventana)."""
    assert settings.STEAM_PRICEOVERVIEW_CRON == "*/30 * * * *"
    assert settings.STEAM_PRICEOVERVIEW_PER_TICK * 48 <= settings.STEAM_BUDGET_PRICEOVERVIEW
    assert st.steam_priceoverview.labels["schedule"] == [{"cron": settings.STEAM_PRICEOVERVIEW_CRON}]


async def test_priceoverview_usa_el_ambito_del_host(make=None):
    handler, _ = _steam({"a": "1.00"})
    state = make_state(handler)
    assert state.gate.scope(steam_market.URL) == "steamcommunity.com"
    raw = await steam_market.priceoverview(state, "a")
    assert raw["success"] is True
