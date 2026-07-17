from unittest.mock import AsyncMock

from rag import router as rag_router


def test_ask_returns_reply_and_sources(client, monkeypatch):
    monkeypatch.setattr(rag_router, "retrieve", AsyncMock(return_value=[
        {"title": "Op nueva", "url": "https://u", "published_at": "2026-07-15T00:00:00Z",
         "content": "Nueva operación."}
    ]))
    monkeypatch.setattr(rag_router, "generate_with_context",
                        AsyncMock(return_value="Subió por la operación."))

    resp = client.post("/rag/ask", json={"question": "por que sube el karambit?"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["reply"] == "Subió por la operación."
    assert data["sources"] == [
        {"title": "Op nueva", "url": "https://u", "published_at": "2026-07-15T00:00:00Z"}
    ]


def test_ask_rejects_empty_question(client):
    resp = client.post("/rag/ask", json={"question": "   "})
    assert resp.status_code == 400


def test_rag_ingest_requires_token(client, monkeypatch):
    monkeypatch.setattr(rag_router, "RAG_INGEST_TOKEN", "secret123")
    resp = client.post("/internal/rag-ingest")
    assert resp.status_code == 401


def test_rag_ingest_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr(rag_router, "RAG_INGEST_TOKEN", "secret123")
    resp = client.post("/internal/rag-ingest", headers={"X-Rag-Ingest-Token": "wrong"})
    assert resp.status_code == 401


def test_rag_ingest_runs_with_valid_token(client, monkeypatch):
    monkeypatch.setattr(rag_router, "RAG_INGEST_TOKEN", "secret123")
    monkeypatch.setattr(rag_router, "ingest",
                        AsyncMock(return_value={"fetched": 3, "new": 1, "chunks": 2}))
    resp = client.post("/internal/rag-ingest", headers={"X-Rag-Ingest-Token": "secret123"})
    assert resp.status_code == 200
    assert resp.json() == {"fetched": 3, "new": 1, "chunks": 2}
