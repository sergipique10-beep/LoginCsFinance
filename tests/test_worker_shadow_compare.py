"""§ 6.3 · Comparación diaria de la convivencia (worker/jobs/shadow_compare.py)."""
import datetime as dt
from types import SimpleNamespace

import fakeredis
import pytest

from worker import settings
from worker.jobs import market_sync as ms
from worker.jobs import shadow_compare as sc
from worker.models import Quote

DAY = dt.date(2026, 10, 18)
NOW = dt.datetime(2026, 10, 19, 0, 25, tzinfo=dt.UTC)


def rows(prices: dict[str, float], delta7d: float | None = 1.0, sold: int = 10) -> list[dict]:
    return [{"name": n, "price_latest": p, "price_delta_7d": delta7d, "sold_24h": sold, "turnover": p * sold}
            for n, p in prices.items()]


def idx(values: dict[str, float]) -> list[dict]:
    return [{"ts": ts, "priceindex": v} for ts, v in values.items()]


SW = [dict(r, price_delta_7d=float(i)) for i, r in enumerate(rows({f"S{i}": 20.0 + i for i in range(12)}))]
IDX_UP = idx({"2026-10-17T23:00:00+00:00": 100.0, "2026-10-18T10:00:00+00:00": 90.0, "2026-10-18T23:00:00+00:00": 101.0})
IDX_DOWN = idx({"2026-10-17T23:00:00+00:00": 100.0, "2026-10-18T23:00:00+00:00": 99.0})
MV = [{"name": "S1", "bucket": "hot"}, {"name": "S2", "bucket": "cold"}]


def test_identico_cumple_todo():
    r = sc.report(DAY, SW, SW, MV, MV, IDX_UP, IDX_UP, {"S1": (21.0, 10)}, 0)
    assert r["ok"] and r["fails"] == ""
    assert r["n"] == 12 and r["mediana_desv_pct"] == 0 and r["p90_desv_pct"] == 0 and r["ratio_mediana"] == 1
    assert r["overlap_trending_top50"] == 1 and r["overlap_movers"] == 1
    assert r["ratio_sold24h_mediana"] == 1 and r["index_sign_match"] is True
    assert r["steam_desv_pct"] == 0 and r["n_steam"] == 1


def test_desviacion_solape_signo_y_cooldown_fallan_por_separado():
    own = rows({f"S{i}": (20.0 + i) * 0.8 for i in range(12)}, delta7d=-1.0)   # ratio 0,8 → 20 % de desviación
    r = sc.report(DAY, own, SW, [MV[0]], MV, IDX_DOWN, IDX_UP, {"S1": (25.2, 20)}, 7200)   # Steam propio +20 %
    assert not r["ok"]
    assert r["mediana_desv_pct"] == 20 and r["ratio_mediana"] == 0.8 and r["steam_desv_pct"] == 20
    assert r["overlap_movers"] == 0.5 and r["index_sign_match"] is False
    assert r["ratio_sold24h_mediana"] == 2 and r["steam_cooldown_last_s"] == 7200
    for k in ("steam_desv_pct", "overlap_movers", "ratio_sold24h", "index_sign", "steam_cooldown", "corr_delta7d"):
        assert k in r["fails"].split(",")
    assert "ratio_mediana" not in r["fails"]      # 0,8 está dentro de la banda: la referencia es consistente


def test_sin_datos_no_certifica_pero_no_revienta():
    r = sc.report(DAY, [], [], [], [], [], [], {}, 0)
    assert r["n"] == 0 and r["mediana_desv_pct"] is None and r["index_sign_match"] is None and not r["ok"]
    assert "ratio_mediana" in r["fails"] and "steam_desv_pct" in r["fails"] and "index_sign" not in r["fails"]


def test_desviacion_solo_en_nombres_de_10_dolares_y_correlacion():
    sw = rows({"caro": 50.0, "barato": 5.0}) + rows({f"C{i}": 30.0 + i for i in range(5)}, delta7d=None)
    for i, r in enumerate(sw):
        r["price_delta_7d"] = float(i)
    own = [dict(r, price_latest=r["price_latest"] * 2) for r in sw if r["name"] == "barato"] \
        + [dict(r) for r in sw if r["name"] != "barato"]
    r = sc.report(DAY, own, sw, [], [], [], [], {}, 0)
    assert r["n"] == 6 and r["mediana_desv_pct"] == 0        # «barato» (5 $) no cuenta aunque se desvíe un 100 %
    assert r["ratio_p10"] is None                             # < 10 pares: sin cuantiles
    assert r["corr_delta7d"] == 1.0 and r["n_delta7d"] == 6


def test_tarea_registrada_a_las_00_25():
    assert sc.shadow_compare.labels["schedule"][0]["cron"] == "25 0 * * *"


@pytest.fixture
def fake(monkeypatch):
    db = SimpleNamespace(tables={"market_trending": SW, settings.MARKET_TRENDING_TABLE: SW,
                                 "market_movers": MV, settings.MARKET_MOVERS_TABLE: MV},
                         idx={"market_cap_history": IDX_UP, settings.MARKET_CAP_HISTORY_TABLE: IDX_UP},
                         upserts=[], ok=[])

    async def select_all(sb, table, columns, page=1000, **eq):
        return db.tables[table]

    async def cap_points_since(sb, table, since_iso):
        return db.idx[table]

    async def upsert(sb, table, rows, on_conflict, batch=500):
        db.upserts.append((table, rows, on_conflict))
        return len(rows)

    async def latest_quotes(pool, since):
        return {"S1": [Quote(NOW, "S1", "steam", 21.0, 12), Quote(NOW, "S1", "skinport", 17.0, None, 3)]}

    async def source_ok(pool, s):
        db.ok.append(s)

    async def push(*a, **k):
        return True
    monkeypatch.setattr(ms, "now_utc", lambda: NOW)
    monkeypatch.setattr(ms.health, "push", push)
    monkeypatch.setattr(sc.sb_store, "select_all", select_all)
    monkeypatch.setattr(sc.sb_store, "cap_points_since", cap_points_since)
    monkeypatch.setattr(sc.sb_store, "upsert", upsert)
    monkeypatch.setattr(sc.market, "latest_quotes", latest_quotes)
    monkeypatch.setattr(ms.market, "source_ok", source_ok)
    return db


async def test_job_escribe_la_fila_de_ayer(fake):
    redis = fakeredis.aioredis.FakeRedis()
    await redis.set("cool:last:steamcommunity.com", 3600)
    res = await sc.run_shadow_compare(SimpleNamespace(redis=redis, pg=object(), supabase=object(), http=None))
    assert res["ok"] and res["date"] == "2026-10-18" and res["fails"] == ""
    (table, rows, key), = fake.upserts
    assert table == settings.SHADOW_REPORT_TABLE and key == "date" and rows[0]["date"] == "2026-10-18"
    assert rows[0]["ratio_sold24h_mediana"] == 1.2 and rows[0]["steam_cooldown_last_s"] == 3600
    assert rows[0]["steam_desv_pct"] == 0 and rows[0]["n_steam"] == 1     # S1: Steam propio 21,0 = steamwebapi 21,0
    assert fake.ok == ["shadow_compare"]


def test_referencia_rota_en_la_cola_baja_falla_por_p10():
    own = rows({f"S{i}": (20.0 + i) * (0.001 if i < 2 else 0.8) for i in range(12)})
    r = sc.report(DAY, own, SW, MV, MV, IDX_UP, IDX_UP, {}, 0)
    assert r["ratio_mediana"] == 0.8 and r["ratio_p10"] < 0.4 and "ratio_p10" in r["fails"]
