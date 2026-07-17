import pytest
from unittest.mock import AsyncMock, MagicMock

from rag import embeddings


@pytest.mark.asyncio
async def test_embed_text_returns_vector(monkeypatch):
    monkeypatch.setattr(embeddings, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(embeddings, "GEMINI_EMBED_MODEL", "gemini-embedding-001")

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"embedding": {"values": [0.1, 0.2, 0.3]}})
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)

    out = await embeddings.embed_text(client, "hola mundo")

    assert out == [0.1, 0.2, 0.3]
    # pide el modelo y dimensionalidad correctos
    _, kwargs = client.post.call_args
    assert kwargs["json"]["output_dimensionality"] == 768


@pytest.mark.asyncio
async def test_embed_text_raises_without_key(monkeypatch):
    monkeypatch.setattr(embeddings, "GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError):
        await embeddings.embed_text(MagicMock(), "x")
