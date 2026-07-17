import pytest
from unittest.mock import AsyncMock, MagicMock

from rag import gemini


@pytest.mark.asyncio
async def test_generate_with_context_includes_chunks(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(gemini, "GEMINI_MODEL", "gemini-flash-latest")

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "candidates": [{"content": {"parts": [{"text": "Subió por la operación."}]}}]
    })
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)

    chunks = [{"title": "Op nueva", "url": "u", "content": "Nueva operación en CS2."}]
    out = await gemini.generate_with_context(client, "por que sube?", chunks)

    assert out == "Subió por la operación."
    body = client.post.call_args.kwargs["json"]
    sent = str(body)
    assert "Nueva operación en CS2." in sent  # el contexto viaja en el prompt


@pytest.mark.asyncio
async def test_generate_with_context_raises_without_key(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError):
        await gemini.generate_with_context(MagicMock(), "x", [])
