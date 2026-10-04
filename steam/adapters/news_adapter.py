"""Adapter de Steam News (`GetNewsForApp/v2`): `appnews.newsitems[]` → `NewsEntry`."""
from functools import partial
from typing import Any

from steam.adapters._common import mappings, require_dict, require_list
from steam.domain.models import NewsEntry
from steam.domain.validators import as_int, as_str

SOURCE = "steam_news"


def adapt_news(raw: Any) -> list[NewsEntry]:
    """Un cuerpo que no es objeto, o sin `appnews.newsitems` como lista, es
    `UnexpectedPayload` (antes: 500 por AttributeError, o `[]` en silencio; CAL-14)."""
    op = "news"
    appnews = require_dict(require_dict(raw, source=SOURCE, op=op).get("appnews"), source=SOURCE, op=f"{op}.appnews")
    items = mappings(require_list(appnews.get("newsitems"), source=SOURCE, op=f"{op}.newsitems"), source=SOURCE, op=op)
    s = partial(as_str, source=SOURCE, op=op)
    return [
        NewsEntry(
            gid=s(n.get("gid"), field="gid"), title=s(n.get("title"), field="title"),
            url=s(n.get("url"), field="url"), contents=s(n.get("contents"), field="contents"),
            date=as_int(n.get("date"), field="date", source=SOURCE, op=op),
            author=s(n.get("author"), field="author"), feed_label=s(n.get("feedlabel"), field="feedlabel"),
            feed_name=s(n.get("feedname"), field="feedname"),
        )
        for n in items
    ]
