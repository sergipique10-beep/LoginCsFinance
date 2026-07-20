from unittest.mock import AsyncMock

from chat import router as chat_router


def test_chat_returns_reply(client, monkeypatch):
    monkeypatch.setattr(chat_router, "generate_with_tools",
                        AsyncMock(return_value="Hola, soy Sharky."))
    resp = client.post("/rag/chat", json={"message": "hola", "history": []})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "Hola, soy Sharky."


def test_chat_rejects_empty_message(client):
    resp = client.post("/rag/chat", json={"message": "   ", "history": []})
    assert resp.status_code == 400
