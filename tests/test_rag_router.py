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


def test_ask_dedups_sources_by_url(client, monkeypatch):
    monkeypatch.setattr(rag_router, "retrieve", AsyncMock(return_value=[
        {"title": "Op nueva", "url": "https://u", "published_at": "2026-07-15T00:00:00Z",
         "content": "Parte 1."},
        {"title": "Op nueva", "url": "https://u", "published_at": "2026-07-15T00:00:00Z",
         "content": "Parte 2."},
    ]))
    monkeypatch.setattr(rag_router, "generate_with_context",
                        AsyncMock(return_value="Subió por la operación."))

    resp = client.post("/rag/ask", json={"question": "por que sube el karambit?"})

    assert resp.status_code == 200
    assert len(resp.json()["sources"]) == 1


def test_ask_rejects_empty_question(client):
    resp = client.post("/rag/ask", json={"question": "   "})
    assert resp.status_code == 400


def test_chat_passes_retrieved_context(client, monkeypatch):
    chunks = [{"title": "CS2 Update", "url": "https://u", "content": "Cache vuelve."}]
    monkeypatch.setattr(rag_router, "retrieve", AsyncMock(return_value=chunks))
    gen = AsyncMock(return_value="Cache volvió al Active Duty.")
    monkeypatch.setattr(rag_router, "generate_reply", gen)

    resp = client.post("/rag/chat", json={"message": "¿por qué volvió Cache?", "history": []})

    assert resp.status_code == 200
    assert resp.json()["reply"] == "Cache volvió al Active Duty."
    assert gen.await_args.kwargs["context_chunks"] == chunks


def test_chat_degrades_when_retrieve_fails(client, monkeypatch):
    monkeypatch.setattr(rag_router, "retrieve", AsyncMock(side_effect=RuntimeError("supabase caído")))
    gen = AsyncMock(return_value="Respuesta general.")
    monkeypatch.setattr(rag_router, "generate_reply", gen)

    resp = client.post("/rag/chat", json={"message": "hola", "history": []})

    assert resp.status_code == 200                       # no se cae por el fallo del RAG
    assert resp.json()["reply"] == "Respuesta general."
    assert gen.await_args.kwargs["context_chunks"] == []  # degradó sin contexto


def test_chat_rejects_empty_message(client):
    resp = client.post("/rag/chat", json={"message": "   ", "history": []})
    assert resp.status_code == 400


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
