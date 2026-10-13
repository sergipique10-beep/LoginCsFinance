"""COST-02 · Piezas de arranque del worker: settings, logs, health, stores, broker, noop, main."""
import logging
from types import SimpleNamespace

import httpx
import pytest

from worker import broker, health, jobs, logs, main, settings
from worker.store import pg, redis, supabase


def test_limites_del_diseno_y_override():
    lim = settings.load_limits("")
    s = lim["steamcommunity.com"]
    assert (s.rate, s.per_s, s.burst, s.reset_timeout, s.steam) == (10, 60, 2, 900, True)
    assert s.emission_ms == 6000
    assert lim["api.steampowered.com"].emission_ms == 1000
    assert lim["api.skinport.com"].emission_ms == 37500
    tuned = settings.load_limits('{"steamcommunity.com": {"rate": 6}, "csfloat.com": {"rate": 1, "per_s": 1, "jitter": [0, 0]}}')
    assert tuned["steamcommunity.com"].rate == 6 and tuned["steamcommunity.com"].burst == 2
    assert tuned["csfloat.com"].jitter == (0, 0)


def test_kuma_tokens():
    assert settings.kuma_tokens("noop=abc, market-sync=def,roto") == {"noop": "abc", "market-sync": "def"}
    assert settings.kuma_tokens("") == {}


def test_secret_filter_enmascara(caplog):
    f = logs.SecretFilter(["SUPERSECRETKEY123", "corta"])
    rec = logging.LogRecord("x", logging.INFO, "", 0, "GET %s key=%s", ("https://a/?key=abc&x=1", "SUPERSECRETKEY123"), None)
    f.filter(rec)
    assert "SUPERSECRETKEY123" not in rec.getMessage() and "key=abc" not in rec.getMessage()
    assert "x=1" in rec.getMessage()


def test_configure_instala_el_filtro(monkeypatch):
    monkeypatch.setenv("STEAM_WEB_API_KEY", "VALVEKEY0000")
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        logs.configure("DEBUG")
        assert any(isinstance(f, logs.SecretFilter) and "VALVEKEY0000" in f.secrets
                   for h in root.handlers for f in h.filters)
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_health_push():
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200)
    ok = await health.push(_client(handler), "noop", True, "todo bien", 12.4, tokens={"noop": "T"}, base_url="http://kuma")
    assert ok and seen[0].path == "/api/push/T" and seen[0].params["status"] == "up" and seen[0].params["ping"] == "12"
    assert await health.push(_client(handler), "otro", False, tokens={"noop": "T"}) is False


async def test_health_push_red_caida_no_lanza():
    def handler(req):
        raise httpx.ConnectError("kuma caído")
    assert await health.push(_client(handler), "noop", True, tokens={"noop": "T"}, base_url="http://kuma") is False


class _Conn:
    async def fetchval(self, q):
        return 1


class _Acq:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *a):
        return False


async def test_pg_select_one_y_pool(monkeypatch):
    assert await pg.select_one(SimpleNamespace(acquire=lambda: _Acq())) is True
    seen = {}

    async def fake_create_pool(dsn, **kw):
        seen.update(dsn=dsn, **kw)
        return "pool"
    monkeypatch.setattr(pg.asyncpg, "create_pool", fake_create_pool)
    assert await pg.create_pool("postgresql://w@h/db") == "pool"
    assert seen["server_settings"]["search_path"] == "worker,public"


async def test_supabase_select_one(monkeypatch):
    class Q:
        def __getattr__(self, name):
            return lambda *a, **k: self

        def execute(self):
            return SimpleNamespace(data=[])
    assert await supabase.select_one(SimpleNamespace(table=lambda name: Q())) is True
    monkeypatch.setattr(supabase, "create_client", lambda url, key: ("sb", url))
    assert supabase.client("https://x.supabase.co", "k") == ("sb", "https://x.supabase.co")


def test_clientes_de_valkey_reconectan():
    a = redis.async_client("redis://:pw@valkey:6379/0")
    s = redis.sync_client("redis://:pw@valkey:6379/0")
    for c in (a, s):
        kw = c.connection_pool.connection_kwargs
        assert kw["health_check_interval"] == 30 and kw["retry"] is not None


async def test_open_y_close_resources(monkeypatch):
    monkeypatch.setattr(settings, "PG_DSN", "")
    monkeypatch.setattr(settings, "SUPABASE_URL", "")
    state = SimpleNamespace()
    await broker.open_resources(state)
    assert state.pg is None and state.supabase is None and state.gate.limit("steamcommunity.com").steam
    await broker.close_resources(state)
    assert state.http.is_closed


async def test_open_resources_con_todo(monkeypatch):
    async def fake_pool(dsn):
        return SimpleNamespace(close=_noop_async)
    monkeypatch.setattr(settings, "PG_DSN", "postgresql://w@h/db")
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_KEY", "k")
    monkeypatch.setattr(broker.pg, "create_pool", fake_pool)
    monkeypatch.setattr(broker.supabase, "client", lambda url, key: "sb")
    state = SimpleNamespace()
    await broker.open_resources(state)
    assert state.supabase == "sb"
    await broker.close_resources(state)


async def _noop_async(*a, **k):
    return None


def test_noop_registrado_cada_minuto():
    assert jobs.noop.task_name == "noop"
    assert jobs.noop.labels["schedule"] == [{"cron": "* * * * *"}]


async def test_run_noop(monkeypatch):
    pushed = {}

    async def fake_push(client, job, ok, msg="", ping_ms=None, **kw):
        pushed.update(job=job, ok=ok, msg=msg)
        return True

    async def ok(_):
        return True

    async def boom(_):
        raise RuntimeError("pg caído")
    monkeypatch.setattr(jobs.health, "push", fake_push)
    monkeypatch.setattr(jobs.supabase, "select_one", ok)
    monkeypatch.setattr(jobs.pg, "select_one", ok)
    res = await jobs.run_noop(SimpleNamespace(http=None, supabase="sb", pg="pool"))
    assert res["ok"] and pushed == {"job": "noop", "ok": True, "msg": "supabase=True pg=True"}
    monkeypatch.setattr(jobs.pg, "select_one", boom)
    res = await jobs.run_noop(SimpleNamespace(http=None, supabase="sb", pg="pool"))
    assert not res["ok"] and "pg caído" in res["pg"] and pushed["ok"] is False
    res = await jobs.run_noop(SimpleNamespace(http=None, supabase=None, pg=None))
    assert not res["ok"]


async def test_noop_task_usa_el_state_del_contexto(monkeypatch):
    async def fake_run(state):
        return {"state": state}
    monkeypatch.setattr(jobs, "run_noop", fake_run)
    assert await jobs.noop.original_func(SimpleNamespace(state="S")) == {"state": "S"}


def test_main_argv(monkeypatch):
    assert main.argv_for("scheduler")[:3] == ["taskiq", "scheduler", "worker.broker:scheduler"]
    with pytest.raises(SystemExit):
        main.argv_for("otro")
    seen = []
    monkeypatch.setattr(main.os, "execvp", lambda f, a: seen.append(a))
    main.main(["worker"])
    assert seen == [main.COMMANDS["worker"]]
