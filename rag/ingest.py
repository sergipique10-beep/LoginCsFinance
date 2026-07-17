"""Ingesta del corpus RAG: fetch de feeds RSS → limpieza → chunking →
embedding de lo nuevo → upsert en Supabase.

Idempotente: dedup por external_id contra la tabla antes de embeddear, así
las corridas repetidas casi no gastan cuota de embeddings.
"""
import logging
from datetime import datetime, timezone

import feedparser
import httpx

from settings import RAG_FEEDS
from steam.mappers import _clean_news_content
from rag import embeddings, repo

logger = logging.getLogger("uvicorn.error")

_CHUNK_MAX_CHARS = 2000
# límite alto: no queremos que _clean_news_content trunque el cuerpo de la noticia.
_CLEAN_MAX = 100_000

_STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
_STEAM_NEWS_COUNT = 20


def chunk_text(text: str, max_chars: int = _CHUNK_MAX_CHARS) -> list[str]:
    """Parte texto en trozos <= max_chars respetando límites de palabra.

    Texto corto → un solo trozo. Texto vacío/blanco → lista vacía.
    """
    text = " ".join(text.split())
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    words = text.split(" ")
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            chunks.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def _entry_published(entry) -> str | None:
    tm = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not tm:
        return None
    return datetime(*tm[:6], tzinfo=timezone.utc).isoformat()


def parse_feed_entries(feeds_xml: list[str]) -> list[dict]:
    """Parsea XML RSS crudo → items normalizados con texto limpio."""
    items: list[dict] = []
    for xml in feeds_xml:
        parsed = feedparser.parse(xml)
        source = (parsed.feed.get("title") or "rss")[:120]
        for e in parsed.entries:
            external_id = e.get("id") or e.get("link") or ""
            if not external_id:
                continue
            raw = e.get("summary") or e.get("description") or ""
            content = _clean_news_content(raw, max_chars=_CLEAN_MAX)
            if not content:
                continue
            items.append({
                "external_id": external_id,
                "title": e.get("title", ""),
                "url": e.get("link", ""),
                "content": content,
                "published_at": _entry_published(e),
                "source": source,
            })
    return items


def parse_steam_news(payload: dict) -> list[dict]:
    """Items de la Steam News API (JSON) → mismos dicts que parse_feed_entries."""
    items: list[dict] = []
    newsitems = (payload.get("appnews") or {}).get("newsitems") or []
    for n in newsitems:
        gid = n.get("gid")
        if not gid:
            continue
        content = _clean_news_content(n.get("contents", ""), max_chars=_CLEAN_MAX)
        if not content:
            continue
        ts = n.get("date")
        published = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
        )
        items.append({
            "external_id": f"steam:{gid}",
            "title": n.get("title", ""),
            "url": n.get("url", ""),
            "content": content,
            "published_at": published,
            "source": n.get("feedlabel") or "steam_news",
        })
    return items


async def _fetch_feeds(client: httpx.AsyncClient) -> list[str]:
    xmls: list[str] = []
    for url in RAG_FEEDS:
        try:
            resp = await client.get(url, timeout=20.0)
            resp.raise_for_status()
            xmls.append(resp.text)
        except httpx.HTTPError as exc:
            logger.warning("rag ingest: fallo al fetch %s: %s", url, exc)
    return xmls


async def _fetch_steam_news(client: httpx.AsyncClient) -> list[dict]:
    try:
        resp = await client.get(
            _STEAM_NEWS_URL,
            params={"appid": 730, "count": _STEAM_NEWS_COUNT, "format": "json"},
            timeout=20.0,
        )
        resp.raise_for_status()
        return parse_steam_news(resp.json())
    except httpx.HTTPError as exc:
        logger.warning("rag ingest: fallo al fetch Steam News: %s", exc)
        return []


async def ingest(client: httpx.AsyncClient) -> dict:
    """Corre una ingesta completa. Devuelve {fetched, new, chunks}."""
    xmls = await _fetch_feeds(client)
    items = parse_feed_entries(xmls)
    items += await _fetch_steam_news(client)
    if not items:
        return {"fetched": 0, "new": 0, "chunks": 0}

    ids = [it["external_id"] for it in items]
    seen = await repo.seen_external_ids(ids)
    fresh = [it for it in items if it["external_id"] not in seen]

    rows: list[dict] = []
    for it in fresh:
        for idx, chunk in enumerate(chunk_text(it["content"])):
            vector = await embeddings.embed_text(client, chunk)
            rows.append({
                "source": it["source"],
                "external_id": it["external_id"],
                "chunk_index": idx,
                "title": it["title"],
                "url": it["url"],
                "content": chunk,
                "published_at": it["published_at"],
                "embedding": vector,
            })

    await repo.upsert_chunks(rows)
    return {"fetched": len(items), "new": len(fresh), "chunks": len(rows)}
