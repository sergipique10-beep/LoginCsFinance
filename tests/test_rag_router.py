from unittest.mock import AsyncMock

from rag import router as rag_router


def test_ask_endpoint_eliminado(client):
    """/rag/ask se retiró: su función la cubre /rag/chat (retrieval + sources)."""
    resp = client.post("/rag/ask", json={"question": "por que sube el karambit?"})
    assert resp.status_code == 404


def test_rag_ingest_requires_token(client, monkeypatch):
    monkeypatch.setattr(rag_router, "RAG_INGEST_TOKEN", "secret123")
    resp = client.post("/internal/rag-ingest")
    assert resp.status_code == 401


def test_rag_ingest_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr(rag_router, "RAG_INGEST_TOKEN", "secret123")
    resp = client.post("/internal/rag-ingest", headers={"X-Rag-Ingest-Token": "wrong"})
    assert resp.status_code == 401


def test_rag_ingest_non_ascii_token_returns_401_not_500(client, monkeypatch):
    monkeypatch.setattr(rag_router, "RAG_INGEST_TOKEN", "secret123")
    # httpx solo acepta str ASCII como valor de header str; para simular un
    # cliente real mandando bytes no-ASCII (p.ej. UTF-8 crudo), pasamos bytes.
    resp = client.post(
        "/internal/rag-ingest",
        headers={"X-Rag-Ingest-Token": "señor".encode("utf-8")},
    )
    assert resp.status_code == 401


def test_rag_ingest_runs_with_valid_token(client, monkeypatch):
    monkeypatch.setattr(rag_router, "RAG_INGEST_TOKEN", "secret123")
    monkeypatch.setattr(rag_router, "ingest",
                        AsyncMock(return_value={"fetched": 3, "new": 1, "chunks": 2}))
    resp = client.post("/internal/rag-ingest", headers={"X-Rag-Ingest-Token": "secret123"})
    assert resp.status_code == 200
    assert resp.json() == {"fetched": 3, "new": 1, "chunks": 2}
