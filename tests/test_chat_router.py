from unittest.mock import AsyncMock

from chat import router as chat_router


def test_chat_returns_reply(client, monkeypatch):
    monkeypatch.setattr(chat_router, "generate_with_sources",
                        AsyncMock(return_value=("Hola, soy Sharky.", [])))
    resp = client.post("/rag/chat", json={"message": "hola", "history": []})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "Hola, soy Sharky."
    assert resp.json()["sources"] == []


def test_chat_expone_sources_deduplicadas(client, monkeypatch):
    """Los fragmentos del RAG viajan como sources[], sin repetir URL."""
    fragmentos = [
        {"title": "Parche 1.2", "url": "https://x/1", "published_at": "2026-07-01"},
        {"title": "Parche 1.2", "url": "https://x/1", "published_at": "2026-07-01"},
        {"title": "Operación", "url": "https://x/2", "published_at": None},
    ]
    monkeypatch.setattr(chat_router, "generate_with_sources",
                        AsyncMock(return_value=("Según las noticias...", fragmentos)))
    resp = client.post("/rag/chat", json={"message": "novedades?", "history": []})
    assert resp.status_code == 200
    sources = resp.json()["sources"]
    assert [s["url"] for s in sources] == ["https://x/1", "https://x/2"]
    assert sources[0]["title"] == "Parche 1.2"


def test_chat_rejects_empty_message(client):
    resp = client.post("/rag/chat", json={"message": "   ", "history": []})
    assert resp.status_code == 400
