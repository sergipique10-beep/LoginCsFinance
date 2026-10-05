import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from alerts import service
from steam.adapters.steam_adapter import adapt_item
from steam.services import price_capture_service as price_capture


def _alert(id, name, direction, threshold, steam_id="u1"):
    return {"id": id, "steam_id": steam_id, "market_hash_name": name,
            "direction": direction, "threshold": threshold}


def _prepare(monkeypatch, alerts, prices, tokens=None, total_active=None, cached=None):
    """Mocks del tick: repo de alertas, lookup por skin, tokens y envío.
    `prices` mapea nombre → item crudo de steamwebapi (o excepción)."""
    monkeypatch.setattr(service.repo, "count_active", AsyncMock(return_value=total_active or len(alerts)))
    monkeypatch.setattr(service.repo, "fetch_active", AsyncMock(return_value=alerts))
    mark_triggered = AsyncMock()
    mark_checked = AsyncMock()
    monkeypatch.setattr(service.repo, "mark_triggered", mark_triggered)
    monkeypatch.setattr(service.repo, "mark_checked", mark_checked)

    async def lookup(client, name):
        v = prices[name]
        if isinstance(v, Exception):
            raise v
        return adapt_item(v)
    lookup_mock = AsyncMock(side_effect=lookup)
    monkeypatch.setattr(price_capture, "lookup_item", lookup_mock)
    # PERF-09: sin precio cacheado por defecto → se ejercita el lookup como antes.
    monkeypatch.setattr(service, "_cached_prices", AsyncMock(return_value=dict(cached or {})))

    monkeypatch.setattr(service.notif_repo, "list_device_tokens_for",
                        AsyncMock(return_value=tokens or {}))
    send = AsyncMock(return_value={"sent": 1, "failed": 0, "pruned": 0})
    monkeypatch.setattr(service, "send_to_tokens", send)
    return lookup_mock, mark_triggered, mark_checked, send


# ── condición ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("direction,threshold,price,expected", [
    ("below", 40.0, 39.99, True),
    ("below", 40.0, 40.0, True),     # inclusivo en el borde
    ("below", 40.0, 40.01, False),
    ("above", 40.0, 40.01, True),
    ("above", 40.0, 40.0, True),
    ("above", 40.0, 39.99, False),
])
def test_condition_met(direction, threshold, price, expected):
    assert service.condition_met(direction, threshold, price) is expected


# ── evaluate_alerts ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_triggers_marks_and_sends_to_owner(monkeypatch):
    alert = _alert(1, "AK-47 | Redline (Field-Tested)", "below", 40.0)
    _, mark_triggered, mark_checked, send = _prepare(
        monkeypatch, [alert],
        prices={"AK-47 | Redline (Field-Tested)": {"pricelatestsell": 38.2}},
        tokens={"u1": ["tok-1", "tok-2"]},
    )

    out = await service.evaluate_alerts(MagicMock())

    assert out == {"evaluated": 1, "triggered": 1, "sent": 1, "errors": 0, "pendientes": 0, "quota_exhausted": False}
    mark_triggered.assert_awaited_once_with(1, 38.2)
    args = send.await_args
    assert args.args[0] == ["tok-1", "tok-2"]
    assert args.args[1] == "AK-47 | Redline (Field-Tested)"
    assert "38.20" in args.args[2] and "40.00" in args.args[2]
    assert args.args[3] == {
        "type": "price_alert", "alertId": "1",
        "marketHashName": "AK-47 | Redline (Field-Tested)", "price": "38.20",
    }
    mark_checked.assert_awaited_once_with([1])


@pytest.mark.asyncio
async def test_not_met_is_only_marked_checked(monkeypatch):
    alert = _alert(1, "X", "below", 40.0)
    _, mark_triggered, mark_checked, send = _prepare(
        monkeypatch, [alert], prices={"X": {"pricelatestsell": 45.0}},
    )

    out = await service.evaluate_alerts(MagicMock())

    assert out["triggered"] == 0
    mark_triggered.assert_not_awaited()
    send.assert_not_awaited()
    mark_checked.assert_awaited_once_with([1])


@pytest.mark.asyncio
async def test_one_lookup_per_skin_even_with_several_alerts(monkeypatch):
    alerts = [_alert(1, "X", "below", 40.0, "u1"), _alert(2, "X", "above", 30.0, "u2"),
              _alert(3, "Y", "below", 10.0, "u1")]
    lookup, *_ = _prepare(
        monkeypatch, alerts,
        prices={"X": {"pricelatestsell": 35.0}, "Y": {"pricelatestsell": 50.0}},
        tokens={"u1": ["t1"], "u2": ["t2"]},
    )

    out = await service.evaluate_alerts(MagicMock())

    assert lookup.await_count == 2                     # X e Y, no 3
    assert out["triggered"] == 2                       # alerta 1 (≤40) y 2 (≥30)


@pytest.mark.asyncio
async def test_mark_triggered_happens_before_send_and_blocks_it_on_failure(monkeypatch):
    """Idempotencia: sin marca no hay envío; así un fallo a medias no duplica."""
    alert = _alert(1, "X", "below", 40.0)
    _, mark_triggered, _, send = _prepare(
        monkeypatch, [alert], prices={"X": {"pricelatestsell": 1.0}}, tokens={"u1": ["t"]},
    )
    mark_triggered.side_effect = RuntimeError("db down")

    out = await service.evaluate_alerts(MagicMock())

    send.assert_not_awaited()
    assert out["errors"] == 1


@pytest.mark.asyncio
async def test_send_failure_does_not_undo_trigger(monkeypatch):
    alert = _alert(1, "X", "below", 40.0)
    _, mark_triggered, _, send = _prepare(
        monkeypatch, [alert], prices={"X": {"pricelatestsell": 1.0}}, tokens={"u1": ["t"]},
    )
    send.side_effect = RuntimeError("fcm down")

    out = await service.evaluate_alerts(MagicMock())

    mark_triggered.assert_awaited_once()
    assert out == {"evaluated": 1, "triggered": 1, "sent": 0, "errors": 1, "pendientes": 0, "quota_exhausted": False}


@pytest.mark.asyncio
async def test_owner_without_tokens_is_triggered_but_nothing_sent(monkeypatch):
    alert = _alert(1, "X", "below", 40.0)
    _, mark_triggered, _, send = _prepare(
        monkeypatch, [alert], prices={"X": {"pricelatestsell": 1.0}}, tokens={},
    )

    out = await service.evaluate_alerts(MagicMock())

    mark_triggered.assert_awaited_once()
    send.assert_not_awaited()
    assert out["triggered"] == 1 and out["sent"] == 0


@pytest.mark.asyncio
async def test_lookup_failure_counts_error_and_still_rotates(monkeypatch):
    alert = _alert(1, "X", "below", 40.0)
    _, mark_triggered, mark_checked, _ = _prepare(
        monkeypatch, [alert], prices={"X": RuntimeError("429")},
    )

    out = await service.evaluate_alerts(MagicMock())

    assert out["errors"] == 1 and out["triggered"] == 0
    mark_triggered.assert_not_awaited()
    mark_checked.assert_awaited_once_with([1])       # avanza la rueda igual


@pytest.mark.asyncio
async def test_item_without_price_is_skipped(monkeypatch):
    alert = _alert(1, "X", "below", 40.0)
    _, mark_triggered, _, _ = _prepare(monkeypatch, [alert], prices={"X": {"pricelatestsell": 0}})

    out = await service.evaluate_alerts(MagicMock())

    assert out["triggered"] == 0 and out["errors"] == 0
    mark_triggered.assert_not_awaited()


@pytest.mark.asyncio
async def test_pendientes_reports_what_is_left_outside_the_batch(monkeypatch):
    alert = _alert(1, "X", "below", 40.0)
    _prepare(monkeypatch, [alert], prices={"X": {"pricelatestsell": 50.0}}, total_active=25)

    out = await service.evaluate_alerts(MagicMock())

    assert out["pendientes"] == 24


# ── create_alert ──────────────────────────────────────────────────────────────

def _prepare_create(monkeypatch, *, active=0, duplicate=False, tracked=True, item=None, cached=None):
    monkeypatch.setattr(service.repo, "count_active", AsyncMock(return_value=active))
    monkeypatch.setattr(service.repo, "exists_active", AsyncMock(return_value=duplicate))
    monkeypatch.setattr(service.price_history_repo, "is_tracked", AsyncMock(return_value=tracked))
    register = AsyncMock()
    monkeypatch.setattr(service.price_history_repo, "register_tracked", register)
    monkeypatch.setattr(price_capture, "lookup_item", AsyncMock(return_value=adapt_item(item) if item else None))
    monkeypatch.setattr(service, "_cached_prices", AsyncMock(return_value=dict(cached or {})))
    create = AsyncMock(return_value={"id": 7})
    monkeypatch.setattr(service.repo, "create", create)
    return create, register


@pytest.mark.asyncio
async def test_create_rounds_threshold_and_persists(monkeypatch):
    create, register = _prepare_create(monkeypatch)

    out = await service.create_alert(MagicMock(), "u1", "X", "below", 40.004)

    assert out == {"id": 7}
    create.assert_awaited_once_with("u1", "X", "below", 40.0)
    register.assert_not_awaited()                     # ya estaba en tracked_skins


@pytest.mark.asyncio
async def test_create_rejects_over_limit(monkeypatch):
    _prepare_create(monkeypatch, active=service.ALERTS_MAX_PER_USER)

    with pytest.raises(service.LimitReached):
        await service.create_alert(MagicMock(), "u1", "X", "below", 40.0)


@pytest.mark.asyncio
async def test_create_rejects_duplicate(monkeypatch):
    _prepare_create(monkeypatch, duplicate=True)

    with pytest.raises(service.Duplicate):
        await service.create_alert(MagicMock(), "u1", "X", "below", 40.0)


@pytest.mark.asyncio
async def test_create_untracked_skin_is_resolved_and_registered(monkeypatch):
    create, register = _prepare_create(monkeypatch, tracked=False, item={"pricelatestsell": 12.0})

    await service.create_alert(MagicMock(), "u1", "Nueva | Skin (Factory New)", "above", 15.0)

    register.assert_awaited_once_with(["Nueva | Skin (Factory New)"], "alert")
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_untracked_unknown_skin_is_rejected(monkeypatch):
    create, _ = _prepare_create(monkeypatch, tracked=False, item={})

    with pytest.raises(service.UnknownItem):
        await service.create_alert(MagicMock(), "u1", "No Existe", "above", 15.0)
    create.assert_not_awaited()


# ── PERF-09: precio desde rankings, /item una vez al día, 402 aborta ─────────

from datetime import datetime, timedelta, timezone

from steam.errors import QuotaExhausted


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


@pytest.mark.asyncio
async def test_cached_price_is_used_without_any_lookup(monkeypatch):
    alert = _alert(1, "X", "below", 40.0)
    lookup, mark_triggered, _, send = _prepare(
        monkeypatch, [alert], prices={}, cached={"X": 39.0}, tokens={"u1": ["t1"]},
    )

    out = await service.evaluate_alerts(MagicMock())

    lookup.assert_not_awaited()
    assert out["triggered"] == 1 and out["sent"] == 1 and out["errors"] == 0
    mark_triggered.assert_awaited_once_with(1, 39.0)


@pytest.mark.asyncio
async def test_uncached_skin_checked_recently_is_not_looked_up_again(monkeypatch):
    alert = {**_alert(1, "X", "below", 40.0), "last_checked_at": _iso(timedelta(hours=-2))}
    lookup, mark_triggered, mark_checked, _ = _prepare(monkeypatch, [alert], prices={"X": {"pricelatest": 10}})

    out = await service.evaluate_alerts(MagicMock())

    lookup.assert_not_awaited()                       # como mucho un /item al día por skin
    assert out["errors"] == 0 and out["triggered"] == 0
    mark_checked.assert_awaited_once_with([1])


@pytest.mark.asyncio
async def test_uncached_skin_not_checked_for_a_day_is_looked_up(monkeypatch):
    alert = {**_alert(1, "X", "below", 40.0), "last_checked_at": _iso(timedelta(hours=-25))}
    lookup, mark_triggered, _, _ = _prepare(monkeypatch, [alert], prices={"X": {"pricelatest": 10}}, tokens={"u1": ["t1"]})

    out = await service.evaluate_alerts(MagicMock())

    lookup.assert_awaited_once()
    assert out["triggered"] == 1


@pytest.mark.asyncio
async def test_quota_exhausted_stops_lookups_but_cached_alerts_still_trigger(monkeypatch):
    alerts = [_alert(1, "A", "below", 40.0), _alert(2, "B", "below", 40.0), _alert(3, "C", "below", 40.0)]
    lookup, mark_triggered, mark_checked, _ = _prepare(
        monkeypatch, alerts,
        prices={"B": QuotaExhausted("402"), "C": {"pricelatest": 1}},
        cached={"A": 30.0}, tokens={"u1": ["t1"]},
    )

    out = await service.evaluate_alerts(MagicMock())

    assert lookup.await_count == 1                     # B revienta, C ya no se intenta
    assert out["quota_exhausted"] is True
    assert out["errors"] == 1 and out["triggered"] == 1
    mark_triggered.assert_awaited_once_with(1, 30.0)
    mark_checked.assert_awaited_once_with([1, 2, 3])   # la rueda avanza igual


@pytest.mark.asyncio
async def test_create_uses_cached_price_and_skips_lookup(monkeypatch):
    register, create = _prepare_create(monkeypatch, tracked=False, cached={"AK": 12.0})

    await service.create_alert(MagicMock(), "u1", "AK", "below", 10.0)

    price_capture.lookup_item.assert_not_awaited()
    register.assert_awaited_once()
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_reports_price_unavailable_when_quota_is_exhausted(monkeypatch):
    _prepare_create(monkeypatch, tracked=False)
    monkeypatch.setattr(price_capture, "lookup_item", AsyncMock(side_effect=QuotaExhausted("402")))

    with pytest.raises(service.PriceUnavailable):
        await service.create_alert(MagicMock(), "u1", "AK", "below", 10.0)


def test_cached_prices_ignores_stale_rows_and_prefers_movers(monkeypatch):
    import asyncio
    fresh, stale = _iso(timedelta(hours=-1)), _iso(timedelta(hours=-30))
    monkeypatch.setattr(service.movers_repo, "fetch_prices", AsyncMock(return_value=[
        {"name": "A", "price_latest": "10.5", "updated_at": fresh},
        {"name": "B", "price_latest": "20", "updated_at": stale},
    ]))
    monkeypatch.setattr(service.trending_repo, "fetch_prices", AsyncMock(return_value=[
        {"name": "A", "price_latest": "99", "updated_at": fresh},
        {"name": "B", "price_latest": "21", "updated_at": fresh},
        {"name": "C", "price_latest": None, "updated_at": fresh},
    ]))

    prices = asyncio.run(service._cached_prices(["A", "B", "C"]))

    assert prices == {"A": 10.5, "B": 21.0}


def test_fetch_active_selects_last_checked_at():
    """Sin esta columna `_lookup_due` cree que ninguna alerta se ha evaluado nunca y el
    tope de un /item al día por skin desaparece (visto en producción el 2026-09-26)."""
    from alerts import repo
    assert "last_checked_at" in repo._COLS


# ── PUSH-10: tick interno ─────────────────────────────────────────────────────


async def test_evaluate_alerts_never_overlaps(monkeypatch):
    """El bucle interno y el POST del workflow comparten proceso: un tick a la vez."""
    running = 0
    peak = 0

    async def slow(_client):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return {"evaluated": 0}

    monkeypatch.setattr(service, "_evaluate_alerts", slow)
    await asyncio.gather(service.evaluate_alerts(None), service.evaluate_alerts(None))
    assert peak == 1


async def test_tick_loop_survives_a_failure_and_keeps_ticking(monkeypatch):
    calls = 0

    async def flaky(_client):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("supabase caído")
        return {"evaluated": 0}

    monkeypatch.setattr(service, "evaluate_alerts", flaky)
    task = asyncio.create_task(service.run_tick_loop(None, interval=0))
    for _ in range(50):
        await asyncio.sleep(0)
        if calls >= 2:
            break
    task.cancel()
    assert calls >= 2
