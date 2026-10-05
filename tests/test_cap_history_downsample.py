"""CAL-06: _downsample es por donde pasa toda la gráfica CAP INDEX HISTORY.
Un fallo aquí no da un 500, da una gráfica rara: por eso se fija con tests."""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

import steam.services.market_service as market
from steam.services.market_service import _CAP_FIELDS, _downsample, _parse_ts

HOUR = timedelta(hours=1)


def _row(ts: str, priceindex=100.0, real=1.0, buyorder=1.0, turnover=1.0) -> dict:
    return {
        "ts": ts,
        "priceindex": priceindex,
        "realpriceindex": real,
        "buyorderpriceindex": buyorder,
        "turnover24h": turnover,
    }


def test_groups_rows_by_bucket():
    rows = [
        _row("2026-09-01T10:00:00Z"),
        _row("2026-09-01T10:30:00Z"),
        _row("2026-09-01T11:00:00Z"),
        _row("2026-09-01T11:30:00Z"),
    ]
    out = _downsample(rows, HOUR)
    assert [p["ts"] for p in out] == ["2026-09-01T10:00:00Z", "2026-09-01T11:00:00Z"]


def test_bucket_boundary_belongs_to_the_next_bucket():
    rows = [_row("2026-09-01T10:59:59Z"), _row("2026-09-01T11:00:00Z")]
    assert len(_downsample(rows, HOUR)) == 2


def test_averages_every_field_within_a_bucket():
    rows = [
        _row("2026-09-01T10:00:00Z", priceindex=100, real=10, buyorder=1, turnover=1000),
        _row("2026-09-01T10:30:00Z", priceindex=200, real=30, buyorder=3, turnover=3000),
    ]
    (point,) = _downsample(rows, HOUR)
    assert point["priceindex"] == 150
    assert point["realpriceindex"] == 20
    assert point["buyorderpriceindex"] == 2
    assert point["turnover24h"] == 2000


def test_none_in_one_row_is_skipped_not_counted_as_zero():
    rows = [
        _row("2026-09-01T10:00:00Z", real=None),
        _row("2026-09-01T10:30:00Z", real=40),
    ]
    (point,) = _downsample(rows, HOUR)
    assert point["realpriceindex"] == 40


def test_none_in_every_row_yields_none():
    rows = [
        _row("2026-09-01T10:00:00Z", real=None),
        _row("2026-09-01T10:30:00Z", real=None),
    ]
    (point,) = _downsample(rows, HOUR)
    assert point["realpriceindex"] is None


def test_missing_key_is_treated_like_none():
    rows = [{"ts": "2026-09-01T10:00:00Z", "priceindex": 100.0}]
    (point,) = _downsample(rows, HOUR)
    assert point["priceindex"] == 100
    assert point["turnover24h"] is None


def test_zero_is_a_value_not_a_missing_one():
    rows = [
        _row("2026-09-01T10:00:00Z", turnover=0),
        _row("2026-09-01T10:30:00Z", turnover=10),
    ]
    (point,) = _downsample(rows, HOUR)
    assert point["turnover24h"] == 5


def test_v_mirrors_priceindex_in_every_point():
    rows = [
        _row("2026-09-01T10:00:00Z", priceindex=100),
        _row("2026-09-01T10:30:00Z", priceindex=200),
        _row("2026-09-01T11:00:00Z", priceindex=300),
    ]
    out = _downsample(rows, HOUR)
    assert [p["v"] for p in out] == [150, 300]
    assert all(p["v"] == p["priceindex"] for p in out)


def test_point_shape_is_the_frontend_contract():
    (point,) = _downsample([_row("2026-09-01T10:00:00Z")], HOUR)
    assert set(point) == {"ts", "v", *_CAP_FIELDS}


def test_output_keeps_ascending_input_order():
    rows = [_row(f"2026-09-0{d}T10:00:00Z", priceindex=d) for d in range(1, 6)]
    out = _downsample(rows, timedelta(days=1))
    assert [p["ts"] for p in out] == sorted(p["ts"] for p in out)
    assert [p["v"] for p in out] == [1, 2, 3, 4, 5]


def test_empty_input_yields_empty_list():
    assert _downsample([], HOUR) == []


def test_six_hour_bucket_starts_on_multiples_of_six():
    rows = [_row("2026-09-01T07:15:00Z"), _row("2026-09-01T13:45:00Z")]
    out = _downsample(rows, timedelta(hours=6))
    assert [p["ts"] for p in out] == ["2026-09-01T06:00:00Z", "2026-09-01T12:00:00Z"]


def test_z_and_explicit_offset_land_in_the_same_bucket():
    rows = [_row("2026-09-01T10:00:00Z"), _row("2026-09-01T10:30:00+00:00")]
    assert len(_downsample(rows, HOUR)) == 1


@pytest.mark.parametrize("ts", [
    "2026-09-01T10:00:00Z",
    "2026-09-01T10:00:00+00:00",
    "2026-09-01T12:00:00+02:00",   # Supabase puede devolver otro offset: mismo instante
    "2026-09-01T10:00:00",         # naive se asume UTC
])
def test_parse_ts_normalises_to_utc(ts):
    dt = _parse_ts(ts)
    assert dt.isoformat() == "2026-09-01T10:00:00+00:00"


# ── Endpoint: qué bucket usa cada timeframe ──────────────────────────────────

def test_endpoint_1m_uses_six_hour_buckets(client, monkeypatch):
    rows = [
        _row("2026-09-01T00:30:00Z", priceindex=100),
        _row("2026-09-01T05:30:00Z", priceindex=200),
        _row("2026-09-01T06:00:00Z", priceindex=300),
    ]
    monkeypatch.setattr(market, "fetch_range", AsyncMock(return_value=rows))

    resp = client.get("/market/cap-history?tf=1m")

    assert resp.status_code == 200
    assert [(p["ts"], p["v"]) for p in resp.json()] == [
        ("2026-09-01T00:00:00Z", 150),
        ("2026-09-01T06:00:00Z", 300),
    ]


def test_endpoint_rejects_unknown_timeframe_without_touching_the_db(client, monkeypatch):
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(market, "fetch_range", fetch)

    resp = client.get("/market/cap-history?tf=10y")

    assert resp.status_code == 400
    fetch.assert_not_awaited()


def test_every_timeframe_has_a_bucket():
    assert set(market._CAP_TF_MAP) == set(market._CAP_BUCKET_MAP)
