"""Mappers de Steam News: limpieza del cuerpo y `NewsItem`. El filtro de alfabeto (UX-05,
`is_readable_news`) y la categoría por fuente (`news_category`) están en `domain/rules.py`."""
import html
import re
from datetime import datetime, timezone

from steam.domain.models import NewsEntry, NewsItem
from steam.domain.rules import news_category


def _clean_news_content(raw: str, max_chars: int = 220) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)           # HTML tags
    text = re.sub(r"\[[^\]]*\]", " ", text)        # BBCode [b], [url=...], [img]
    text = re.sub(r"\{[^}]*\}", " ", text)         # {STEAM_CLAN_IMAGE}, {h2}, etc.
    text = html.unescape(text)                     # &amp; &nbsp; &#39; etc.
    text = re.sub(r"https?://\S+", "", text)       # full URLs
    text = re.sub(r"(?<!\w)/\S+", "", text)        # /path or //cdn tokens
    text = re.sub(r"\s*\\\s*", " ", text)          # backslash separators
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0]
    return text


def _map_news_item(entry: NewsEntry, index: int, image_url: str = "") -> NewsItem:
    feedlabel = entry.feed_label if entry.feed_label is not None else "NEWS"
    category_color = news_category(entry.feed_name, entry.feed_label).color

    try:
        date_str = datetime.fromtimestamp(entry.date, tz=timezone.utc).strftime("%Y-%m-%d") if entry.date is not None else ""
    except (ValueError, OSError, OverflowError):
        date_str = ""

    author = (entry.author or "").strip()
    excerpt = _clean_news_content(entry.contents or "")

    return {
        "id":            entry.gid if entry.gid is not None else str(index),
        "category":      feedlabel.upper(),
        "categoryColor": category_color,
        "title":         entry.title or "",
        "source":        author if author else feedlabel,
        "date":          date_str,
        "imageUrl":      image_url,
        "featured":      index == 0,
        "url":           entry.url or "",
        "content":       excerpt,
    }
