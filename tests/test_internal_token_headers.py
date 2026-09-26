"""SEC-05: un token de cabecera con bytes no-ASCII es un 401, nunca un 500.

Starlette decodifica cabeceras como latin-1, así que un byte alto crudo llega
como `str` no-ASCII y `secrets.compare_digest` sobre `str` lanza TypeError.
httpx rechaza cabeceras `str` no-ASCII en el cliente, por eso aquí van bytes.
"""
import secrets
from unittest.mock import AsyncMock

import pytest

import alerts.router as alerts_router
import auth.service as auth_service
import notifications.router as notif_router
import rag.router as rag_router
import steam.routes.market as market_routes

RAW = b"caf\xe9"   # é en latin-1: no-ASCII

ENDPOINTS = [
    ("/internal/cap-tick",     b"X-Cap-Token",         market_routes, "CAP_TICK_TOKEN"),
    ("/internal/trending-tick", b"X-Cap-Token",        market_routes, "CAP_TICK_TOKEN"),
    ("/internal/enrich-tick",  b"X-Cap-Token",         market_routes, "CAP_TICK_TOKEN"),
    ("/internal/movers-tick",  b"X-Cap-Token",         market_routes, "CAP_TICK_TOKEN"),
    ("/internal/price-tick",   b"X-Price-Tick-Token",  market_routes, "PRICE_TICK_TOKEN"),
    ("/internal/news-tick",    b"X-News-Tick-Token",   notif_router,  "NEWS_TICK_TOKEN"),
    ("/internal/broadcast",    b"X-Broadcast-Token",   notif_router,  "BROADCAST_TOKEN"),
    ("/internal/rag-ingest",   b"X-Rag-Ingest-Token",  rag_router,    "RAG_INGEST_TOKEN"),
    ("/internal/alerts-tick",  b"X-Alerts-Tick-Token", alerts_router, "ALERTS_TICK_TOKEN"),
]


@pytest.mark.parametrize("path,header,module,attr", ENDPOINTS, ids=[e[0] for e in ENDPOINTS])
def test_non_ascii_header_token_is_401_not_500(client, monkeypatch, path, header, module, attr):
    monkeypatch.setattr(module, attr, "secret123")
    body = {"title": "x", "body": "y"} if path.endswith("broadcast") else None

    resp = client.post(path, headers={header: RAW}, json=body)

    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("path,header,module,attr", ENDPOINTS, ids=[e[0] for e in ENDPOINTS])
def test_missing_and_wrong_tokens_are_401(client, monkeypatch, path, header, module, attr):
    monkeypatch.setattr(module, attr, "secret123")
    body = {"title": "x", "body": "y"} if path.endswith("broadcast") else None

    assert client.post(path, json=body).status_code == 401
    assert client.post(path, headers={header.decode(): "wrong"}, json=body).status_code == 401


def test_endpoint_is_disabled_when_its_token_is_unset(client, monkeypatch):
    monkeypatch.setattr(notif_router, "NEWS_TICK_TOKEN", "")
    assert client.post("/internal/news-tick", headers={"X-News-Tick-Token": ""}).status_code == 401


def test_token_matches_uses_constant_time_comparison_over_bytes(monkeypatch):
    """Mutación cazada: sustituir compare_digest por `==` deja el contador a cero."""
    calls: list[tuple] = []
    real = secrets.compare_digest   # capturar antes de parchear: el módulo es el mismo

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth_service.secrets, "compare_digest", spy)

    assert auth_service.token_matches("secret", "secret") is True
    assert auth_service.token_matches("café", "secret") is False       # no-ASCII: sin TypeError
    assert calls and all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)
