import pytest
from unittest.mock import AsyncMock, MagicMock

from steam import price_capture


def test_canonical_price_prefers_latestsell():
    assert price_capture._canonical_price(
        {"pricelatestsell": 43.15, "pricelatest": 41.35, "pricemedian": 42.71}
    ) == 43.15


def test_canonical_price_falls_back_when_zero():
    assert price_capture._canonical_price(
        {"pricelatestsell": 0, "pricelatest": 0, "pricemedian": 42.71}
    ) == 42.71


def test_canonical_price_none_when_all_missing():
    assert price_capture._canonical_price({}) is None


@pytest.mark.asyncio
async def test_seed_tracked_only_when_empty(monkeypatch):
    monkeypatch.setattr(price_capture.repo, "count_tracked", AsyncMock(return_value=0))
    reg = AsyncMock()
    monkeypatch.setattr(price_capture.repo, "register_tracked", reg)

    n = await price_capture.seed_tracked()

    assert n > 0
    reg.assert_awaited_once()
    assert reg.await_args.args[1] == "top_n"


@pytest.mark.asyncio
async def test_seed_tracked_skips_when_populated(monkeypatch):
    monkeypatch.setattr(price_capture.repo, "count_tracked", AsyncMock(return_value=5))
    reg = AsyncMock()
    monkeypatch.setattr(price_capture.repo, "register_tracked", reg)

    n = await price_capture.seed_tracked()

    assert n == 0
    reg.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_snapshots_and_marks(monkeypatch):
    monkeypatch.setattr(price_capture, "PRICE_LOOKUP_CAP", 400)
    monkeypatch.setattr(price_capture.repo, "fetch_tracked",
                        AsyncMock(return_value=["AK-47 | Redline (Field-Tested)"]))
    upsert = AsyncMock()
    mark = AsyncMock()
    monkeypatch.setattr(price_capture.repo, "upsert_prices", upsert)
    monkeypatch.setattr(price_capture.repo, "mark_captured", mark)
    # el lookup por-nombre devuelve un item con precio y volumen
    monkeypatch.setattr(price_capture, "_lookup_item",
                        AsyncMock(return_value={"pricelatestsell": 43.15, "sold24h": 69}))

    out = await price_capture.capture(MagicMock())

    assert out["captured"] == 1
    row = upsert.await_args.args[0][0]
    assert row["market_hash_name"] == "AK-47 | Redline (Field-Tested)"
    assert row["price"] == 43.15
    assert row["volume"] == 69
    mark.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_skips_item_without_price(monkeypatch):
    monkeypatch.setattr(price_capture, "PRICE_LOOKUP_CAP", 400)
    monkeypatch.setattr(price_capture.repo, "fetch_tracked",
                        AsyncMock(return_value=["Bad | Skin (Field-Tested)"]))
    upsert = AsyncMock()
    monkeypatch.setattr(price_capture.repo, "upsert_prices", upsert)
    monkeypatch.setattr(price_capture.repo, "mark_captured", AsyncMock())
    monkeypatch.setattr(price_capture, "_lookup_item",
                        AsyncMock(return_value={"pricelatestsell": 0}))

    out = await price_capture.capture(MagicMock())

    assert out["captured"] == 0
    assert out["skipped"] == 1
    upsert.assert_awaited_once()
    assert upsert.await_args.args[0] == []  # nada que upsertear


@pytest.mark.asyncio
async def test_capture_counts_errors(monkeypatch):
    monkeypatch.setattr(price_capture, "PRICE_LOOKUP_CAP", 400)
    monkeypatch.setattr(price_capture.repo, "fetch_tracked",
                        AsyncMock(return_value=["X | Y (Field-Tested)"]))
    monkeypatch.setattr(price_capture.repo, "upsert_prices", AsyncMock())
    monkeypatch.setattr(price_capture.repo, "mark_captured", AsyncMock())
    monkeypatch.setattr(price_capture, "_lookup_item",
                        AsyncMock(side_effect=RuntimeError("boom")))

    out = await price_capture.capture(MagicMock())

    assert out["errors"] == 1
    assert out["captured"] == 0
