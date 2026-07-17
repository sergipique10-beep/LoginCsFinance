import pytest
from unittest.mock import MagicMock

from rag import repo


@pytest.mark.asyncio
async def test_match_chunks_calls_rpc(monkeypatch):
    fake_resp = MagicMock(data=[{"id": 1, "similarity": 0.9, "content": "c",
                                 "source": "s", "title": "t", "url": "u",
                                 "published_at": None}])
    fake_client = MagicMock()
    fake_client.rpc.return_value.execute.return_value = fake_resp
    monkeypatch.setattr(repo, "get_supabase", lambda: fake_client)

    out = await repo.match_chunks([0.1, 0.2], k=3)

    assert out[0]["id"] == 1
    fake_client.rpc.assert_called_once_with(
        "match_rag_chunks", {"query_embedding": [0.1, 0.2], "match_count": 3}
    )


@pytest.mark.asyncio
async def test_seen_external_ids_returns_present_subset(monkeypatch):
    fake_resp = MagicMock(data=[{"external_id": "a"}, {"external_id": "a"}])
    fake_client = MagicMock()
    (fake_client.table.return_value.select.return_value
        .in_.return_value.execute.return_value) = fake_resp
    monkeypatch.setattr(repo, "get_supabase", lambda: fake_client)

    out = await repo.seen_external_ids(["a", "b"])

    assert out == {"a"}


@pytest.mark.asyncio
async def test_upsert_chunks_noop_on_empty(monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: fake_client)

    await repo.upsert_chunks([])

    fake_client.table.assert_not_called()
