import pytest
from unittest.mock import AsyncMock, MagicMock

from rag import retrieval


@pytest.mark.asyncio
async def test_retrieve_embeds_and_matches(monkeypatch):
    monkeypatch.setattr(retrieval.embeddings, "embed_text",
                        AsyncMock(return_value=[0.5] * 768))
    match = AsyncMock(return_value=[{"content": "c", "title": "t",
                                     "url": "u", "similarity": 0.8}])
    monkeypatch.setattr(retrieval.repo, "match_chunks", match)

    out = await retrieval.retrieve(MagicMock(), "por que sube el karambit", k=3)

    assert out[0]["content"] == "c"
    match.assert_awaited_once_with([0.5] * 768, 3)


@pytest.mark.asyncio
async def test_retrieve_empty_query_returns_empty(monkeypatch):
    embed = AsyncMock()
    monkeypatch.setattr(retrieval.embeddings, "embed_text", embed)
    out = await retrieval.retrieve(MagicMock(), "   ")
    assert out == []
    embed.assert_not_awaited()
