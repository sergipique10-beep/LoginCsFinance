import httpx
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


def test_chat_traduce_429_de_gemini_a_429_propio(client, monkeypatch):
    """La cuota agotada no es una caída: 429, no 502 genérico (PERF-04).

    El free tier son 20 req/día por proyecto y el loop de tools gasta 2-4 por
    mensaje, así que es el fallo más probable en producción. Si se presenta como
    502 el usuario aprende que la app se rompe, cuando solo se acabó el cupo.
    """
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/x")
    response = httpx.Response(429, text='{"error":{"message":"quota exceeded"}}', request=request)
    monkeypatch.setattr(
        chat_router, "generate_with_sources",
        AsyncMock(side_effect=httpx.HTTPStatusError("429", request=request, response=response)),
    )

    resp = client.post("/rag/chat", json={"message": "hola", "history": []})

    assert resp.status_code == 429
    assert "límite" in resp.json()["detail"]
    # Gemini da 429 también por el límite por minuto: el texto no promete "mañana".
    assert "diaria" not in resp.json()["detail"]


def test_chat_mantiene_502_para_otros_errores_de_gemini(client, monkeypatch):
    """Un 500 de Gemini sí es una caída: sigue siendo 502."""
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/x")
    response = httpx.Response(500, text="boom", request=request)
    monkeypatch.setattr(
        chat_router, "generate_with_sources",
        AsyncMock(side_effect=httpx.HTTPStatusError("500", request=request, response=response)),
    )

    resp = client.post("/rag/chat", json={"message": "hola", "history": []})

    assert resp.status_code == 502


def test_status_refleja_el_flag(client, monkeypatch):
    monkeypatch.setattr(chat_router, "CHAT_ENABLED", True)
    assert client.get("/rag/chat/status").json() == {"enabled": True}

    monkeypatch.setattr(chat_router, "CHAT_ENABLED", False)
    assert client.get("/rag/chat/status").json() == {"enabled": False}


def test_chat_apagado_devuelve_404(client, monkeypatch):
    """Con CHAT_ENABLED=false el endpoint no existe para el cliente.

    404 y no 403: apagado por configuración no debe anunciar que el endpoint
    está ahí esperando permisos.
    """
    monkeypatch.setattr(chat_router, "CHAT_ENABLED", False)
    monkeypatch.setattr(chat_router, "generate_with_sources",
                        AsyncMock(return_value=("no deberia llamarse", [])))

    resp = client.post("/rag/chat", json={"message": "hola", "history": []})

    assert resp.status_code == 404
    chat_router.generate_with_sources.assert_not_awaited()


def test_chat_apagado_da_404_tambien_sin_autenticar(monkeypatch):
    """Sin token y con el chat apagado: 404, nunca 401.

    Regresión real: con el guard dentro del cuerpo de la función, `require_jwt`
    se evaluaba primero y un anónimo recibía 401 — que confirma que el endpoint
    existe y solo le faltan credenciales. Verificado contra un uvicorn de verdad,
    no solo con el fixture (que va autenticado por `dependency_overrides`).
    """
    from fastapi.testclient import TestClient
    import main as main_module

    monkeypatch.setattr(main_module, "fetch_static_images", AsyncMock())
    monkeypatch.setattr(chat_router, "CHAT_ENABLED", False)

    # Sin dependency_overrides: este cliente es anónimo a propósito.
    with TestClient(main_module.app) as anon:
        resp = anon.post("/rag/chat", json={"message": "hola", "history": []})

    assert resp.status_code == 404, (
        f"esperaba 404 y llegó {resp.status_code}: el flag debe evaluarse antes que require_jwt"
    )

    # Control positivo: sin él, el 404 de arriba también saldría si la ruta no
    # existiera o cambiara de path. Con el flag encendido el anónimo ve 401.
    monkeypatch.setattr(chat_router, "CHAT_ENABLED", True)
    with TestClient(main_module.app) as anon:
        assert anon.post("/rag/chat", json={"message": "hola", "history": []}).status_code == 401
        # El status también exige sesión: era el único endpoint sin auth ni rate limit.
        assert anon.get("/rag/chat/status").status_code == 401
