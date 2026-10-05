"""SEC-16: /item/history tiene bucket propio (60/60 s por IP) y ninguna ruta que no
sea /auth/* comparte contador con auth (10/60 s). Antes, abrir ~5 detalles de skin
(2 llamadas a /item/history cada uno) agotaba el cupo de auth y un refresh que
coincidiera recibía 429."""
import time
from unittest.mock import AsyncMock

from chat import router as chat_router
from stores import ITEM_HISTORY_RATE_LIMIT_CALLS, RATE_LIMIT_CALLS, _rate_store
from steam.cache.history_cache import _item_history_cache
from steam.cache.user_cache import _news_cache

AUTH_KEY = "testclient"   # clave del bucket de auth para la IP de TestClient


def _history(client, days):
    return client.get("/item/history", params={"name": "AK-47 | Redline (Field-Tested)", "days": days})


def test_opening_15_skin_details_does_not_429_nor_touch_auth(client):
    # Caché llena: el test mide el limiter, no steamwebapi.
    for days in (30, 365):
        _item_history_cache[f"AK-47 | Redline (Field-Tested):10:steam:{days}"] = ([], time.monotonic())
    for _ in range(15):                      # 15 detalles = 30 llamadas
        assert _history(client, 30).status_code == 200
        assert _history(client, 365).status_code == 200
    assert AUTH_KEY not in _rate_store       # el cupo de auth sigue intacto
    _item_history_cache.clear()


def test_item_history_budget_rejects_past_its_limit(client):
    _item_history_cache["AK-47 | Redline (Field-Tested):10:steam:30"] = ([], time.monotonic())
    for _ in range(ITEM_HISTORY_RATE_LIMIT_CALLS):
        assert _history(client, 30).status_code == 200
    assert _history(client, 30).status_code == 429
    _item_history_cache.clear()


def test_news_does_not_consume_auth_budget(client):
    _news_cache[5] = ([], time.monotonic())
    for _ in range(RATE_LIMIT_CALLS):
        assert client.get("/news/cs2").status_code == 200
    assert AUTH_KEY not in _rate_store
    _news_cache.clear()


def test_chat_does_not_consume_auth_budget(client, monkeypatch):
    monkeypatch.setattr(chat_router, "generate_with_sources", AsyncMock(return_value=("ok", [])))
    for _ in range(RATE_LIMIT_CALLS):
        assert client.post("/rag/chat", json={"message": "hola", "history": []}).status_code == 200
    assert AUTH_KEY not in _rate_store
