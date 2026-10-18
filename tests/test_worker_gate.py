"""COST-02 · Gate GCRA, cooldown y presupuestos (worker/net/gate.py)."""
import asyncio
import datetime as dt
import shutil
import socket
import subprocess
import sys
import textwrap
import time

import fakeredis
import pytest

from worker.net.gate import Gate, GateTimeout, HostCoolingDown, UnknownHost
from worker.settings import HostLimit

STEAM = "steamcommunity.com"
LIMITS = {STEAM: HostLimit(rate=10, per_s=60, burst=2, jitter=(0.5, 2.0), steam=True),
          STEAM + "/inventory": HostLimit(rate=6, per_s=600, burst=2, jitter=(0, 0), steam=True, cooldown_s=600),
          "api.skinport.com": HostLimit(rate=8, per_s=300, jitter=(0, 0))}


def _gate(server, **kw):
    return Gate(fakeredis.aioredis.FakeRedis(server=server), LIMITS, **kw)


async def test_gcra_rafaga_y_espera():
    g = _gate(fakeredis.FakeServer())
    now = 1_000_000
    assert await g.try_acquire(STEAM, now) == 0
    assert await g.try_acquire(STEAM, now) == 0          # burst 2
    assert await g.try_acquire(STEAM, now) == 6000       # 10/min → 6 s por petición
    assert await g.try_acquire(STEAM, now + 5999) == 1
    assert await g.try_acquire(STEAM, now + 6000) == 0


async def test_dos_clientes_comparten_cubo():
    server = fakeredis.FakeServer()
    a, b = _gate(server), _gate(server)
    now = 5_000_000
    assert await a.try_acquire(STEAM, now) == 0
    assert await b.try_acquire(STEAM, now) == 0
    assert await a.try_acquire(STEAM, now) > 0
    assert await b.try_acquire(STEAM, now) > 0


async def test_host_sin_limite_no_pasa():
    with pytest.raises(UnknownHost):
        await _gate(fakeredis.FakeServer()).acquire("evil.example.com")


async def test_acquire_duerme_lo_que_pide_el_gcra_mas_jitter():
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    g = _gate(fakeredis.FakeServer(), sleep=sleep, rand=lambda lo, hi: hi)
    await g.acquire("api.skinport.com")
    assert sleeps == []                                   # jitter (0, 0) en Skinport
    await g.acquire(STEAM)
    assert sleeps == [2.0]                                # solo el jitter
    calls = iter([3000, 0])
    g.try_acquire = lambda host, now_ms=0: _ret(next(calls))
    await g.acquire(STEAM)
    assert sleeps == [2.0, 3.0, 2.0]


async def _ret(v):
    return v


async def test_acquire_respeta_max_wait():
    g = _gate(fakeredis.FakeServer())
    g.try_acquire = lambda host, now_ms=0: _ret(120_000)
    with pytest.raises(GateTimeout):
        await g.acquire(STEAM, max_wait_s=60)


async def test_cooldown_dobla_con_tope():
    g = _gate(fakeredis.FakeServer())
    assert [await g.open_cooldown(STEAM) for _ in range(5)] == [7200, 14400, 21600, 21600, 21600]
    with pytest.raises(HostCoolingDown) as e:
        await g.acquire(STEAM)
    assert e.value.remaining_s > 21000


async def test_cooldown_dobla_aunque_el_anterior_haya_expirado():
    # El 429 real llega en la primera petición tras expirar el cooldown: la escalada tiene que sobrevivir.
    g = _gate(fakeredis.FakeServer())
    assert await g.open_cooldown(STEAM) == 7200
    await g.redis.delete(f"cool:{STEAM}")          # expiró
    assert await g.cooldown_left(STEAM) == 0
    assert await g.open_cooldown(STEAM) == 14400
    await g.redis.delete(f"cool:{STEAM}", f"cool:last:{STEAM}")   # 24 h sin 429: vuelve a empezar
    assert await g.open_cooldown(STEAM) == 7200


async def test_presupuesto_diario():
    g = _gate(fakeredis.FakeServer())
    day = dt.date(2026, 10, 13)
    assert [await g.try_spend("priceoverview", 2, day) for _ in range(3)] == [True, True, False]
    assert await g.spent("priceoverview", day) == 2
    assert await g.spent("priceoverview", dt.date(2026, 10, 14)) == 0
    assert Gate.budget_key("x", day) == "budget:x:20261013"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not shutil.which("redis-server"), reason="sin redis-server en PATH")
def test_dos_procesos_reales_comparten_cubo():
    """Dos procesos del SO, un Redis real y el reloj de Redis: entre los dos, solo pasa el burst."""
    port = _free_port()
    srv = subprocess.Popen(["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"],  # noqa: S603, S607
                           stdout=subprocess.DEVNULL)
    try:
        time.sleep(0.5)
        code = textwrap.dedent(f"""
            import asyncio, redis.asyncio as r
            from worker.net.gate import Gate
            from worker.settings import HostLimit
            async def m():
                g = Gate(r.Redis(port={port}), {{"h": HostLimit(rate=1, per_s=3600, burst=2)}})
                print(sum([1 for _ in range(3) if await g.try_acquire("h") == 0]))
            asyncio.run(m())
        """)
        procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)  # noqa: S603
                 for _ in range(2)]
        granted = [int(p.communicate(timeout=30)[0].strip()) for p in procs]
        assert sum(granted) == 2
    finally:
        srv.terminate()
        srv.wait()


def test_reloj_de_valkey_por_defecto():
    """Sin now_ms el script usa TIME: dos llamadas seguidas tras el burst esperan ~emisión."""
    async def m():
        g = _gate(fakeredis.FakeServer())
        assert await g.try_acquire(STEAM) == 0
        assert await g.try_acquire(STEAM) == 0
        return await g.try_acquire(STEAM)
    assert 5000 < asyncio.run(m()) <= 6000


def test_scope_por_ruta_declarada():
    g = _gate(fakeredis.FakeServer())
    assert g.scope(f"https://{STEAM}/inventory/7656/730/2") == STEAM + "/inventory"
    assert g.scope(f"https://{STEAM}/market/priceoverview/?x=1") == STEAM
    assert g.scope("https://api.skinport.com/v1/items") == "api.skinport.com"
    assert g.scope("https://desconocido.test/inventory") == "desconocido.test"  # limit() lo rechaza luego


async def test_inventario_y_priceoverview_no_comparten_cubo():
    g = _gate(fakeredis.FakeServer())
    inv, host = STEAM + "/inventory", STEAM
    assert [await g.try_acquire(inv, now_ms=1_000) for _ in range(3)] == [0, 0, 100_000]
    assert await g.try_acquire(host, now_ms=1_000) == 0
    assert await g.cooldown_left(host) == 0 and await g.cooldown_left(inv) == 0
    await g.open_cooldown(inv)
    assert await g.cooldown_left(inv) > 0 and await g.cooldown_left(host) == 0


async def test_cooldown_a_punto_de_caducar_se_espera():
    # El cron llega ~1 s antes de que caduque un cooldown múltiplo exacto del cron (11:00:00 con 1 s, 2026-10-18).
    slept = []

    async def sleep(s):
        slept.append(s)

    g = _gate(fakeredis.FakeServer(), sleep=sleep)
    await g.redis.set(f"cool:{STEAM}", 7200, ex=2)
    await g.acquire(STEAM)
    assert slept[0] == 3
    await g.redis.set(f"cool:{STEAM}", 7200, ex=60)
    with pytest.raises(HostCoolingDown):
        await g.acquire(STEAM)


async def test_cooldown_del_inventario_es_el_suyo_y_no_toca_al_de_priceoverview():
    # COST-00: el inventario se recupera en ≤ 10 min; con el cooldown global de 2 h un 429 dejaba sin inventario 2 h.
    g = _gate(fakeredis.FakeServer())
    inv = STEAM + "/inventory"
    assert [await g.open_cooldown(inv) for _ in range(3)] == [600, 1200, 2400]
    assert await g.open_cooldown(STEAM) == 7200
    assert await g.cooldown_left(STEAM) > 7000 and await g.cooldown_left(inv) < 2500
