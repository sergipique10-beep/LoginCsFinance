"""COST-07 · Decodificador, extract(), fuentes de Steam y cola de inventarios (lado worker)."""
import asyncio
import os
import struct
from types import SimpleNamespace

import fakeredis
import httpx
import pytest
import stamina

from worker import main, settings
from worker.decode import certificate
from worker.decode import inventory as dec
from worker.jobs import inventory as job
from worker.net.breaker import Breakers
from worker.net.gate import Gate, HostCoolingDown
from worker.sources import steam_inventory, steam_profile
from worker.store import inventory as inv_store
from worker.store import supabase as sb_store

FAST = {h: settings.HostLimit(rate=1000, per_s=1, burst=1000, jitter=(0, 0), steam=lim.steam)
        for h, lim in settings.DEFAULT_LIMITS.items()}


@pytest.fixture(autouse=True)
def _fast():
    with stamina.set_testing(True, attempts=3):
        yield


def _varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def make_cert(wear, paintindex=418, seed=661, defindex=507, key=0x5C):
    bits = struct.unpack("<I", struct.pack("<f", wear))[0]
    proto = b"".join(_varint(f << 3) + _varint(v) for f, v in ((3, defindex), (4, paintindex), (7, bits), (8, seed)))
    proto += _varint((12 << 3) | 2) + _varint(2) + b"\x08\x01"       # sticker: length-delimited, se salta
    proto += _varint((20 << 3) | 5) + b"\x01\x02\x03\x04"            # fixed32 desconocido
    plain = b"\x00" + proto + certificate.checksum(proto)
    return (bytes([key]) + bytes(b ^ key for b in plain[1:])).hex()


def test_decode_certificado():
    d = certificate.decode(make_cert(0.0123456))
    assert (d["defindex"], d["paintindex"], d["paintseed"], d["checksum_ok"]) == (507, 418, 661, True)
    assert abs(d["paintwear"] - 0.0123456) < 1e-6
    with pytest.raises(ValueError):
        certificate.decode("0011")
    with pytest.raises(ValueError):
        certificate.parse_proto(b"\x80")            # varint truncado
    with pytest.raises(ValueError):
        certificate.parse_proto(b"\x0b")            # wire type 3


def inventory_payload(wear=0.07):
    cert = make_cert(wear)
    return {
        "assets": [{"assetid": "1", "classid": "c1", "instanceid": "0", "amount": "1"},
                   {"assetid": "2", "classid": "c2", "instanceid": "0", "amount": "3"},
                   {"assetid": "3", "classid": "zz", "instanceid": "0"}],
        "descriptions": [
            {"classid": "c1", "instanceid": "0", "market_hash_name": "★ Karambit | Doppler (Factory New)",
             "name": "★ Karambit | Doppler", "icon_url": "abc", "tradable": 1, "marketable": 1,
             "tags": [{"category": "Exterior", "localized_tag_name": "Factory New"},
                      {"category": "Rarity", "localized_tag_name": "Covert", "color": "eb4b4b"},
                      {"category": "Quality", "localized_tag_name": "★"},
                      {"category": "Type", "localized_tag_name": "Knife"},
                      {"category": "Weapon", "localized_tag_name": "Karambit"}],
             "actions": [{"link": "steam://rungame/730/%propid:6%"}]},
            {"classid": "c2", "instanceid": "0", "market_hash_name": "Revolution Case", "tradable": 0,
             "marketable": 1, "tags": [{"category": "Type", "localized_tag_name": "Container"}]}],
        "asset_properties": [{"assetid": "1", "asset_properties": [
            {"propertyid": 2, "float_value": str(wear)}, {"propertyid": 1, "int_value": "661"},
            {"propertyid": 6, "string_value": cert}, {"propertyid": 99, "int_value": "1"}]}],
    }


def test_extract_y_fase_doppler():
    raw = inventory_payload()
    certs = dec.certificates(raw)
    decoded, failed = dec.decode_certs(certs, {})
    items = dec.extract(raw, decoded)
    knife, case = items
    assert len(items) == 2 and failed == 0
    assert (knife["float_value"], knife["paint_seed"], knife["paint_index"], knife["phase"]) == (0.07, 661, 418, "Phase 1")
    assert knife["is_star"] and knife["rarity_color"] == "eb4b4b" and knife["exterior"] == "Factory New"
    assert (case["amount"], case["tradable"], case["float_value"], case["phase"]) == (3, False, None, None)
    assert dec.decode_missing_ratio(items) == 0.0
    assert dec.decode_missing_ratio([{"exterior": "FN", "float_value": None}]) == 1.0
    assert dec.decode_missing_ratio([]) == 0.0
    # La caché evita volver a decodificar; un certificado roto cuenta como fallo.
    again, failed = dec.decode_certs({**certs, "9": "zz"}, decoded)
    assert again["1"] is decoded["1"] and failed == 1


class Stub:
    """Supabase y Postgres falsos que registran lo que el worker escribe."""

    def __init__(self, pending=None):
        self.pending = pending or []
        self.calls = []


def make_state(handler, stub=None, with_pg=True):
    server = fakeredis.FakeServer()
    r = fakeredis.aioredis.FakeRedis(server=server)
    return SimpleNamespace(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), redis=r,
                           gate=Gate(r, FAST), breakers=Breakers(fakeredis.FakeRedis(server=server), FAST),
                           pg="pool" if with_pg else None, supabase=stub or Stub())


@pytest.fixture
def stores(monkeypatch):
    log = []

    async def pending(sb, limit=100):
        return sb.pending

    async def set_status(sb, ids, status):
        log.append(("status", ids, status))

    async def finish(sb, kind, steam_id, status, error=None):
        log.append(("finish", kind, steam_id, status, error))

    async def upsert(sb, table, rows, on_conflict, batch=500):
        log.append(("upsert", table, rows))
        return len(rows)
    for n, f in dict(pending_requests=pending, set_status=set_status, finish=finish, upsert=upsert).items():
        monkeypatch.setattr(job.sb_store, n, f)

    async def upsert_decoded(pool, rows):
        log.append(("decoded", [r[0] for r in rows]))

    async def replace_assets(pool, sid, items):
        log.append(("assets", sid, len(items)))

    async def delete_user(pool, sid):
        log.append(("pg_delete", sid))
    monkeypatch.setattr(job.inv_store, "upsert_decoded", upsert_decoded)
    monkeypatch.setattr(job.inv_store, "replace_assets", replace_assets)
    monkeypatch.setattr(job.inv_store, "delete_user", delete_user)
    return log


async def test_fetch_pagina_con_referer():
    seen = []
    pages = iter([{"assets": [{"assetid": "1"}], "more_items": 1, "last_assetid": "1"}, {"assets": [{"assetid": "2"}]}])

    def h(req):
        seen.append((req.headers.get("referer"), req.url.params.get("start_assetid")))
        return httpx.Response(200, json=next(pages))
    raw = await steam_inventory.fetch(make_state(h), "765")
    assert [a["assetid"] for a in raw["assets"]] == ["1", "2"]
    assert seen == [("https://steamcommunity.com/profiles/765/inventory/", None),
                    ("https://steamcommunity.com/profiles/765/inventory/", "1")]


async def test_inventario_privado_y_no_disponible():
    with pytest.raises(steam_inventory.InventoryPrivate):
        await steam_inventory.fetch(make_state(lambda r: httpx.Response(403)), "1")
    with pytest.raises(steam_inventory.InventoryUnavailable) as e:
        await steam_inventory.fetch(make_state(lambda r: httpx.Response(400)), "1")
    assert e.value.status == 400


async def test_429_de_steam_abre_cooldown():
    st = make_state(lambda r: httpx.Response(429))
    with pytest.raises(Exception) as e:
        await steam_inventory.fetch(st, "1")
    assert job.error_code(e.value) == "rate_limit"
    with pytest.raises(HostCoolingDown):
        await steam_inventory.fetch(st, "1")


async def test_perfiles_en_lotes(monkeypatch):
    monkeypatch.setenv("STEAM_WEB_API_KEY", "k" * 32)
    calls = []

    def h(req):
        ids = req.url.params["steamids"].split(",")
        calls.append(len(ids))
        return httpx.Response(200, json={"response": {"players": [{"steamid": i, "personastate": 1} for i in ids]}})
    out = await steam_profile.summaries(make_state(h), [str(i) for i in range(150)])
    assert calls == [100, 50] and len(out) == 150
    assert steam_profile.to_row(out[0])["persona_state"] == 1
    monkeypatch.delenv("STEAM_WEB_API_KEY")
    with pytest.raises(steam_profile.MissingKey):
        await steam_profile.summaries(make_state(h), ["1"])


async def test_cola_prioridad_y_dedupe(stores):
    stub = Stub(pending=[{"id": 1, "kind": "inventory", "steam_id": "A", "prio": 2, "created_at": "2026-10-13T10:00:00+00:00"},
                         {"id": 2, "kind": "inventory", "steam_id": "B", "prio": 0, "created_at": "2026-10-13T11:00:00+00:00"},
                         {"id": 3, "kind": "inventory", "steam_id": "A", "prio": 0, "created_at": "2026-10-13T12:00:00+00:00"},
                         {"id": 4, "kind": "raro", "steam_id": "C", "prio": 0}])
    st = make_state(lambda r: httpx.Response(200), stub)
    assert await job.ingress_once(st) == 3
    assert ("status", [1, 2, 3], "queued") in stores
    order = [m.decode() for m, _ in await st.redis.zrange(job.QUEUE, 0, -1, withscores=True)]
    assert order == ["inventory:B", "inventory:A"]            # A subió a prio 0 (ZADD LT) pero es posterior


async def test_sin_supabase_no_hay_cola(stores):
    st = make_state(lambda r: httpx.Response(200))
    st.supabase = None
    assert await job.ingress_once(st) == 0 and stores == []


async def test_procesa_inventario_con_cache_de_certificados(stores):
    st = make_state(lambda r: httpx.Response(200, json=inventory_payload()))
    await st.redis.zadd(job.QUEUE, {"inventory:765": 1})
    res = await job.fetch_once(st)
    assert res["ok"] and res["items"] == 2 and res["decode_missing_ratio"] == 0.0
    up = next(c for c in stores if c[0] == "upsert")
    assert up[1] == "inventory_snapshots_shadow" and up[2][0]["steam_id"] == "765"
    assert ("decoded", ["1"]) in stores and ("assets", "765", 2) in stores
    assert ("finish", "inventory", "765", "done", None) in stores
    # Segunda vez: el certificado sale de Valkey, no se vuelve a escribir en asset_decoded.
    stores.clear()
    await st.redis.zadd(job.QUEUE, {"inventory:765": 1})
    await job.fetch_once(st)
    assert ("decoded", []) in stores
    assert await job.fetch_once(st) is None


async def test_errores_cierran_la_peticion_con_su_codigo(stores):
    st = make_state(lambda r: httpx.Response(403))
    await st.redis.zadd(job.QUEUE, {"inventory:1": 1})
    assert (await job.fetch_once(st))["error"] == "private"
    assert ("finish", "inventory", "1", "error", "private") in stores
    assert job.error_code(steam_inventory.InventoryUnavailable(404)) == "http_404"
    assert job.error_code(RuntimeError()) == "upstream"


async def test_inflight_evita_doble_descarga(stores):
    st = make_state(lambda r: httpx.Response(200, json=inventory_payload()))
    await st.redis.set("inv:inflight:inventory:7", "1")
    await st.redis.zadd(job.QUEUE, {"inventory:7": 1})
    assert (await job.fetch_once(st))["skipped"] == "inflight"


async def test_perfil_y_borrado(stores, monkeypatch):
    monkeypatch.setenv("STEAM_WEB_API_KEY", "k" * 32)
    st = make_state(lambda r: httpx.Response(200, json={"response": {"players": [{"steamid": "9", "personaname": "M"}]}}))
    await st.redis.zadd(job.QUEUE, {"profile:9": 0, "delete:9": 5})
    assert (await job.fetch_once(st))["profiles"] == 1
    assert next(c for c in stores if c[0] == "upsert")[1] == "steam_profiles"
    assert (await job.fetch_once(st))["deleted"] and ("pg_delete", "9") in stores
    empty = make_state(lambda r: httpx.Response(200, json={"response": {"players": []}}))
    await empty.redis.zadd(job.QUEUE, {"profile:8": 0})
    assert (await job.fetch_once(empty))["error"] == "http_404"


async def test_bucle_run_para_con_stop(stores, monkeypatch):
    pushed = []

    async def push(*a, **k):
        pushed.append(a[1])
        return True
    monkeypatch.setattr(job.health, "push", push)
    st = make_state(lambda r: httpx.Response(200, json=inventory_payload()), Stub(pending=[
        {"id": 1, "kind": "inventory", "steam_id": "5", "prio": 0, "created_at": "x"}]))
    stop = asyncio.Event()
    task = asyncio.create_task(job.run(st, stop, poll_s=0.01, idle_s=0.01))
    for _ in range(200):
        if ("finish", "inventory", "5", "done", None) in stores:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert ("finish", "inventory", "5", "done", None) in stores and pushed == ["inventory"]


def test_main_modo_inventory(monkeypatch):
    ran = []

    async def fake_main():
        ran.append(True)
    monkeypatch.setattr(job, "main", fake_main)
    main.main(["inventory"])
    assert ran == [True]
    assert job.score(0, "bad") < job.score(1, "2026-01-01T00:00:00+00:00")


async def test_supabase_helpers_de_la_cola():
    calls = []

    class Q:
        def __init__(self, data=None):
            self.data = data

        def __getattr__(self, name):
            def f(*a, **k):
                calls.append((name, a))
                return self
            return f

        def execute(self):
            return SimpleNamespace(data=[{"id": 1}])
    sb = SimpleNamespace(table=lambda n: Q())
    assert await sb_store.pending_requests(sb) == [{"id": 1}]
    await sb_store.set_status(sb, [], "queued")
    await sb_store.set_status(sb, [1], "queued")
    await sb_store.finish(sb, "inventory", "1", "error", "private")
    assert ("in_", ("status", ["pending", "queued"])) in calls


DSN = os.environ.get("WORKER_PG_TEST_DSN")


@pytest.mark.skipif(not DSN, reason="sin WORKER_PG_TEST_DSN (Timescale con deploy/vps/schema.sql)")
def test_store_inventario_contra_timescale():
    from worker.store import pg

    async def m():
        pool = await pg.create_pool(DSN)
        try:
            items = dec.extract(inventory_payload(), dec.decode_certs(dec.certificates(inventory_payload()), {})[0])
            await inv_store.replace_assets(pool, "765", items)
            await inv_store.replace_assets(pool, "765", items[:1])
            d = certificate.decode(make_cert(0.07))
            await inv_store.upsert_decoded(pool, [("1", d, "hex")])
            await inv_store.upsert_decoded(pool, [("1", d, "hex2")])
            async with pool.acquire() as c:
                assert await c.fetchval("select count(*) from inventory_assets where steam_id='765'") == 1
                assert await c.fetchval("select certificate from asset_decoded where assetid='1'") == "hex2"
            await inv_store.delete_user(pool, "765")
            async with pool.acquire() as c:
                assert await c.fetchval("select count(*) from inventory_assets") == 0
        finally:
            await pool.close()
    asyncio.run(m())
