import pytest
from unittest.mock import MagicMock

from steam import price_history_repo as repo


@pytest.mark.asyncio
async def test_register_tracked_noop_on_empty(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    await repo.register_tracked([], "top_n")
    client.table.assert_not_called()


@pytest.mark.asyncio
async def test_register_tracked_upserts_rows(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    await repo.register_tracked(["AK-47 | Redline (Field-Tested)"], "inventory")
    args, kwargs = client.table.return_value.upsert.call_args
    rows = args[0]
    assert rows[0]["market_hash_name"] == "AK-47 | Redline (Field-Tested)"
    assert rows[0]["source"] == "inventory"
    assert kwargs.get("ignore_duplicates") is True
    assert kwargs.get("on_conflict") == "market_hash_name"


@pytest.mark.asyncio
async def test_register_tracked_refresca_last_seen(monkeypatch):
    """PERF-11: re-registrar una skin existente la mantiene viva (poda blanda)."""
    client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    await repo.register_tracked(["a", "b"], "trending")
    payload = client.table.return_value.update.call_args.args[0]
    assert "last_seen" in payload
    assert "inventory_seen_at" not in payload     # solo /inventory sube de prioridad
    client.table.return_value.update.return_value.in_.assert_called_once_with(
        "market_hash_name", ["a", "b"]
    )


@pytest.mark.asyncio
async def test_register_tracked_inventario_marca_inventory_seen_at(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    await repo.register_tracked(["a"], "inventory")
    payload = client.table.return_value.update.call_args.args[0]
    assert payload["inventory_seen_at"] == payload["last_seen"]


@pytest.mark.asyncio
async def test_register_tracked_trocea_el_update(monkeypatch):
    """El `in_` va en la URL: un inventario grande no puede ir en una sola."""
    client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    await repo.register_tracked([f"s{i}" for i in range(250)], "inventory")
    in_ = client.table.return_value.update.return_value.in_
    assert [len(c.args[1]) for c in in_.call_args_list] == [100, 100, 50]


@pytest.mark.asyncio
async def test_fetch_tracked_lee_la_cola_por_prioridad(monkeypatch):
    client = MagicMock()
    chain = (client.table.return_value.select.return_value
             .order.return_value.order.return_value.limit.return_value)
    chain.execute.return_value = MagicMock(data=[{"market_hash_name": "a"}, {"market_hash_name": "b"}])
    monkeypatch.setattr(repo, "get_supabase", lambda: client)

    out = await repo.fetch_tracked(50)

    assert out == ["a", "b"]
    client.table.assert_called_with("price_tick_queue")
    sel = client.table.return_value.select.return_value
    sel.order.assert_called_once_with("prioridad", desc=False)
    sel.order.return_value.order.assert_called_once_with(
        "last_captured", desc=False, nullsfirst=True
    )
    sel.order.return_value.order.return_value.limit.assert_called_once_with(50)


@pytest.mark.asyncio
async def test_count_captured_on(monkeypatch):
    client = MagicMock()
    (client.table.return_value.select.return_value.eq.return_value
     .execute.return_value) = MagicMock(count=42)
    monkeypatch.setattr(repo, "get_supabase", lambda: client)

    assert await repo.count_captured_on("2026-10-10") == 42
    client.table.assert_called_with("tracked_skins")
    client.table.return_value.select.return_value.eq.assert_called_once_with(
        "last_captured", "2026-10-10"
    )


@pytest.mark.asyncio
async def test_upsert_prices_noop_on_empty(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    await repo.upsert_prices([])
    client.table.assert_not_called()


@pytest.mark.asyncio
async def test_mark_captured_sets_date(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    await repo.mark_captured(["a", "b"], "2026-07-17")
    client.table.return_value.update.assert_called_once_with({"last_captured": "2026-07-17"})
    client.table.return_value.update.return_value.in_.assert_called_once_with(
        "market_hash_name", ["a", "b"]
    )


@pytest.mark.asyncio
async def test_count_tracked(monkeypatch):
    client = MagicMock()
    client.table.return_value.select.return_value.execute.return_value = MagicMock(count=7)
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    assert await repo.count_tracked() == 7
