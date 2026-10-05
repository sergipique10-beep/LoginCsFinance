"""PERF-06: /news/cs2 cachea la respuesta 30 min.

Sin caché cada petición iba a Steam y scrapeaba el og:image de cada noticia:
5,6–12,3 s medidos desde Render. Es la query lenta de la tab Home.
"""
from unittest.mock import AsyncMock, MagicMock

import main
import steam.routes.news as news_module
from steam.api import news_client
from stores import NEWS_CACHE_TTL, _news_cache


def _fake_steam_client(calls: list) -> MagicMock:
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"appnews": {"newsitems": [
        {"gid": "1", "title": "Parche", "url": "https://store.steampowered.com/news/1", "contents": "x", "date": 1700000000},
    ]}}

    async def get(*args, **kwargs):
        calls.append(args)
        return resp

    client = MagicMock(); client.get = get; client.aclose = AsyncMock()  # el lifespan cierra el cliente
    return client


def test_second_call_within_ttl_does_not_hit_steam(client, monkeypatch):
    _news_cache.clear()
    calls: list = []
    monkeypatch.setattr(main.app.state, "http_client", _fake_steam_client(calls))
    monkeypatch.setattr(news_client, "fetch_og_image", AsyncMock(return_value="https://img/1.jpg"))

    first = client.get("/news/cs2")
    second = client.get("/news/cs2")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(calls) == 1


def test_cache_expires_after_ttl(client, monkeypatch):
    _news_cache.clear()
    calls: list = []
    monkeypatch.setattr(main.app.state, "http_client", _fake_steam_client(calls))
    monkeypatch.setattr(news_client, "fetch_og_image", AsyncMock(return_value=""))

    assert client.get("/news/cs2").status_code == 200
    items, ts = _news_cache[5]
    _news_cache[5] = (items, ts - NEWS_CACHE_TTL - 1)   # envejecer la entrada

    assert client.get("/news/cs2").status_code == 200
    assert len(calls) == 2


def test_cache_is_keyed_by_count(client, monkeypatch):
    _news_cache.clear()
    calls: list = []
    monkeypatch.setattr(main.app.state, "http_client", _fake_steam_client(calls))
    monkeypatch.setattr(news_client, "fetch_og_image", AsyncMock(return_value=""))

    client.get("/news/cs2?count=5")
    client.get("/news/cs2?count=3")

    assert len(calls) == 2
