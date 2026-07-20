import pytest
from unittest.mock import AsyncMock, MagicMock

from rag import generation
from llm import gemini as llm_gemini


@pytest.mark.asyncio
async def test_generate_with_context_includes_chunks(monkeypatch):
    monkeypatch.setattr(llm_gemini, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(llm_gemini, "GEMINI_MODEL", "gemini-flash-latest")

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "candidates": [{"content": {"parts": [{"text": "Subió por la operación."}]}}]
    })
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)

    chunks = [{"title": "Op nueva", "url": "u", "content": "Nueva operación en CS2."}]
    out = await generation.generate_with_context(client, "por que sube?", chunks)

    assert out == "Subió por la operación."
    body = client.post.call_args.kwargs["json"]
    assert "Nueva operación en CS2." in str(body)


@pytest.mark.asyncio
async def test_generate_with_context_raises_without_key(monkeypatch):
    monkeypatch.setattr(llm_gemini, "GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError):
        await generation.generate_with_context(MagicMock(), "x", [])
