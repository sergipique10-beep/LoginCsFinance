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


def _resp(text):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={"candidates": [{"content": {"parts": [{"text": text}]}}]})
    return r


@pytest.mark.asyncio
async def test_generate_reply_injects_context(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "k")
    client = MagicMock()
    client.post = AsyncMock(return_value=_resp("Cache volvió al Active Duty."))

    chunks = [{"title": "CS2 Update", "url": "https://u",
               "content": "Added Cache to the Active Duty Map Pool. Removed Overpass."}]
    out = await gemini.generate_reply(client, "¿por qué volvió Cache?", [], context_chunks=chunks)

    assert out == "Cache volvió al Active Duty."
    body = client.post.call_args.kwargs["json"]
    last_text = body["contents"][-1]["parts"][0]["text"]
    assert "CONTEXTO" in last_text
    assert "MENSAJE DEL USUARIO" in last_text
    assert "Added Cache" in last_text            # el contenido del chunk viaja
    assert "¿por qué volvió Cache?" in last_text  # el mensaje también


@pytest.mark.asyncio
async def test_generate_reply_without_context_is_plain_message(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "k")
    client = MagicMock()
    client.post = AsyncMock(return_value=_resp("¡Hola!"))

    out = await gemini.generate_reply(client, "hola", [])

    assert out == "¡Hola!"
    body = client.post.call_args.kwargs["json"]
    last_text = body["contents"][-1]["parts"][0]["text"]
    assert last_text == "hola"                    # sin bloque CONTEXTO
    assert "CONTEXTO" not in last_text


@pytest.mark.asyncio
async def test_generate_reply_empty_chunks_is_plain_message(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "k")
    client = MagicMock()
    client.post = AsyncMock(return_value=_resp("ok"))

    await gemini.generate_reply(client, "hola", [], context_chunks=[])

    last_text = client.post.call_args.kwargs["json"]["contents"][-1]["parts"][0]["text"]
    assert last_text == "hola"
