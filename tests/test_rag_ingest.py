import pytest
from unittest.mock import AsyncMock, MagicMock

from rag import ingest

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Karambit sube</title>
    <link>https://blog.cs/karambit</link>
    <guid>https://blog.cs/karambit</guid>
    <description>El precio del Karambit subio por la nueva operacion.</description>
    <pubDate>Tue, 15 Jul 2026 10:00:00 +0000</pubDate>
  </item>
</channel></rss>"""


def test_chunk_text_short_is_single_chunk():
    assert ingest.chunk_text("hola mundo") == ["hola mundo"]


def test_chunk_text_empty_is_empty():
    assert ingest.chunk_text("   ") == []


def test_chunk_text_long_splits():
    text = "palabra " * 1000  # ~8000 chars
    chunks = ingest.chunk_text(text, max_chars=2000)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)


def test_parse_feed_entries_extracts_fields():
    items = ingest.parse_feed_entries([RSS])
    assert len(items) == 1
    it = items[0]
    assert it["external_id"] == "https://blog.cs/karambit"
    assert it["title"] == "Karambit sube"
    assert "Karambit" in it["content"]


STEAM_NEWS = {
    "appnews": {"newsitems": [
        {"gid": "555", "title": "Operación nueva", "url": "https://store.steam/op",
         "contents": "Valve anuncia una nueva operación con cajas.",
         "date": 1752566400, "feedlabel": "Steam Community"},
    ]}
}


def test_parse_steam_news_extracts_fields():
    items = ingest.parse_steam_news(STEAM_NEWS)
    assert len(items) == 1
    it = items[0]
    assert it["external_id"] == "steam:555"
    assert it["title"] == "Operación nueva"
    assert "operación" in it["content"].lower()
    assert it["published_at"] is not None


def test_parse_steam_news_empty_payload():
    assert ingest.parse_steam_news({}) == []


@pytest.mark.asyncio
async def test_ingest_embeds_only_new(monkeypatch):
    monkeypatch.setattr(ingest, "RAG_FEEDS", ["http://feed"])
    # http_client.get devuelve el RSS
    resp = MagicMock(text=RSS, status_code=200)
    resp.raise_for_status = MagicMock()
    client = MagicMock()

    news_resp = MagicMock(status_code=200)
    news_resp.raise_for_status = MagicMock()
    news_resp.json = MagicMock(return_value=STEAM_NEWS)

    async def _get(url, **kw):
        return news_resp if "GetNewsForApp" in url else resp

    client.get = AsyncMock(side_effect=_get)
    # nada visto aun → el item es nuevo
    monkeypatch.setattr(ingest.repo, "seen_external_ids", AsyncMock(return_value=set()))
    upsert = AsyncMock()
    monkeypatch.setattr(ingest.repo, "upsert_chunks", upsert)
    monkeypatch.setattr(ingest.embeddings, "embed_text",
                        AsyncMock(return_value=[0.0] * 768))

    out = await ingest.ingest(client)

    assert out["new"] == 2
    assert out["chunks"] >= 1
    upsert.assert_awaited_once()
    rows = upsert.await_args.args[0]
    assert rows[0]["embedding"] == [0.0] * 768
    assert rows[0]["external_id"] == "https://blog.cs/karambit"


@pytest.mark.asyncio
async def test_ingest_skips_seen(monkeypatch):
    monkeypatch.setattr(ingest, "RAG_FEEDS", ["http://feed"])
    resp = MagicMock(text=RSS, status_code=200)
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    monkeypatch.setattr(ingest.repo, "seen_external_ids",
                        AsyncMock(return_value={"https://blog.cs/karambit"}))
    embed = AsyncMock(return_value=[0.0] * 768)
    monkeypatch.setattr(ingest.embeddings, "embed_text", embed)
    monkeypatch.setattr(ingest.repo, "upsert_chunks", AsyncMock())

    out = await ingest.ingest(client)

    assert out["new"] == 0
    embed.assert_not_awaited()
