"""Mappers de Steam News: `NewsItem` (la limpieza del cuerpo está en utils/strings). El filtro de alfabeto (UX-05,
`is_readable_news`) y la categoría por fuente (`news_category`) están en `domain/rules.py`."""
from datetime import datetime, timezone

from steam.domain.models import NewsEntry, NewsItem
from steam.domain.rules import news_category
from steam.utils.strings import clean_news_content

# rag/ingest.py y notifications/service.py lo importan con este nombre (CLEAN-19 lo mueve
# a utils/strings sin tocar esos paquetes).
_clean_news_content = clean_news_content


def _map_news_item(entry: NewsEntry, index: int, image_url: str = "") -> NewsItem:
    feedlabel = entry.feed_label if entry.feed_label is not None else "NEWS"
    category_color = news_category(entry.feed_name, entry.feed_label).color

    try:
        date_str = datetime.fromtimestamp(entry.date, tz=timezone.utc).strftime("%Y-%m-%d") if entry.date is not None else ""
    except (ValueError, OSError, OverflowError):
        date_str = ""

    author = (entry.author or "").strip()
    excerpt = clean_news_content(entry.contents or "")

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
