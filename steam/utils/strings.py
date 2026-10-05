"""Strings: limpieza del cuerpo de una noticia de Steam y claves en minúsculas.
Sin dependencias internas."""
import html
import re


def clean_news_content(raw: str, max_chars: int = 220) -> str:
    """Texto plano y acotado a partir del cuerpo de Steam News (HTML, BBCode, macros
    `{STEAM_CLAN_IMAGE}`, URLs). `max_chars` corta en la última palabra entera."""
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


def lower_key(value: str | None) -> str:
    """Forma en minúsculas para claves de caché; None y "" dan ""."""
    return (value or "").lower()
