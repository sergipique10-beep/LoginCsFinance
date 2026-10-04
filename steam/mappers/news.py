"""Mappers de Steam News: limpieza del cuerpo, filtro de alfabeto (UX-05) y `NewsItem`."""
import html
import re
from datetime import datetime, timezone

from steam.domain.models import NewsItem


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


# UX-05: la Steam News API (appid 730) no admite filtro de idioma — devuelve lo
# que publica cada partner, y los medios rusos y chinos publican en su idioma.
# `feedlabel` identifica la fuente, no el idioma, así que no sirve para filtrar.
# Se mira el texto: si una fracción apreciable del titular es cirílico o CJK, la
# noticia es ilegible para el usuario objetivo y se descarta.
_NON_LATIN_RE = re.compile(
    r"[Ѐ-ӿ"      # cirílico
    r"一-鿿"       # han (chino / kanji)
    r"぀-ヿ"       # kana japonés
    r"가-힯]"      # hangul coreano
)

# Fracción de caracteres no latinos por encima de la cual se descarta. 0.2 deja
# pasar un titular en inglés con una palabra o un nombre propio en otro alfabeto,
# y descarta el que está escrito entero en él.
_NON_LATIN_THRESHOLD = 0.2


def is_readable_news(item: dict) -> bool:
    """False si el titular está mayoritariamente en un alfabeto no latino.

    Se mira solo el titular: es lo que el usuario lee en la lista, y el cuerpo
    puede traer markup y nombres propios que ensucian la proporción.
    """
    title = (item.get("title") or "").strip()
    if not title:
        return True  # sin titular no hay nada que juzgar; que decida el resto

    letters = [c for c in title if c.isalpha()]
    if not letters:
        return True  # solo números o símbolos: no es un idioma

    non_latin = sum(1 for c in letters if _NON_LATIN_RE.match(c))
    return (non_latin / len(letters)) <= _NON_LATIN_THRESHOLD


def _map_news_item(item: dict, index: int, image_url: str = "") -> NewsItem:
    feedname  = item.get("feedname", "").lower()
    feedlabel = item.get("feedlabel", "NEWS")

    if "blog" in feedname or "valve" in feedname:
        category_color = "4a9eff"
    elif any(x in feedname for x in ("hltv", "liquipedia", "esport")):
        category_color = "8847ff"
    else:
        category_color = "f0c040"

    try:
        date_str = datetime.fromtimestamp(item["date"], tz=timezone.utc).strftime("%Y-%m-%d")
    except (KeyError, ValueError, OSError):
        date_str = ""

    author = item.get("author", "").strip()
    excerpt = _clean_news_content(item.get("contents", ""))

    return {
        "id":            str(item.get("gid", index)),
        "category":      feedlabel.upper(),
        "categoryColor": category_color,
        "title":         item.get("title", ""),
        "source":        author if author else feedlabel,
        "date":          date_str,
        "imageUrl":      image_url,
        "featured":      index == 0,
        "url":           item.get("url", ""),
        "content":       excerpt,
    }
