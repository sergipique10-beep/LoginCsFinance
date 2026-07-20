import pytest
from unittest.mock import AsyncMock, MagicMock

from llm import gemini


@pytest.mark.asyncio
async def test_call_posts_and_returns_json(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(gemini, "GEMINI_MODEL", "gemini-flash-latest")
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)

    data = await gemini.call(client, {"contents": []})

    assert data["candidates"][0]["content"]["parts"][0]["text"] == "hi"
    assert "generateContent" in client.post.call_args.args[0]


@pytest.mark.asyncio
async def test_call_raises_without_key(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError):
        await gemini.call(MagicMock(), {})


def test_extract_text():
    assert gemini.extract_text([{"content": {"parts": [{"text": "hola"}]}}]) == "hola"
    assert gemini.extract_text([]) is None
    assert gemini.extract_text([{"content": {"parts": [{"functionCall": {}}]}}]) is None


def test_extract_function_call():
    fc = {"functionCall": {"name": "x", "args": {}}}
    assert gemini.extract_function_call([{"content": {"parts": [fc]}}]) == fc
    assert gemini.extract_function_call([]) is None
    assert gemini.extract_function_call([{"content": {"parts": [{"text": "hi"}]}}]) is None
