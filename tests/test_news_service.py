"""CLEAN-15: `news_service.get_cs2_news` devuelve `Fetched` y los fallos de la fuente
suben tipados (la ruta los traduce con `http_error_for`)."""
import httpx
import pytest

from steam.errors import InvalidPayload, QuotaExhausted, RateLimited, SourceUnavailable, UnexpectedPayload
from steam.services import news_service as news_service

NEWS = {"appnews": {"newsitems": [
    {"gid": "1", "title": "Release Notes", "url": "", "contents": "", "date": 0},
]}}


@pytest.fixture
def http(steam_api):
    import main
    return main.app.state.http_client


async def test_ok(steam_api, http):
    steam_api.on("GetNewsForApp/v2/", json=NEWS)
    out = await news_service.get_cs2_news(http, 1)
    assert (out.status, out.reason) == ("ok", None)
    assert [n["title"] for n in out.data] == ["Release Notes"]


async def test_solo_ilegibles_es_partial(steam_api, http):
    steam_api.on("GetNewsForApp/v2/", json={"appnews": {"newsitems": [
        {"gid": "1", "title": "Обновление", "url": "", "contents": "", "date": 0},
    ]}})
    out = await news_service.get_cs2_news(http, 1)
    assert (out.status, out.reason) == ("partial", "no_readable_news")
    assert len(out.data) == 1


@pytest.mark.parametrize("route, exc", [
    ({"status": 402}, QuotaExhausted),
    ({"status": 429}, RateLimited),
    ({"exc": httpx.ConnectError("down")}, SourceUnavailable),
    ({"content": b"<html>"}, InvalidPayload),
    ({"json": []}, UnexpectedPayload),
])
async def test_fallos_de_la_fuente_suben_tipados(steam_api, http, route, exc):
    steam_api.on("GetNewsForApp/v2/", **route)
    with pytest.raises(exc):
        await news_service.get_cs2_news(http, 1)
    assert not news_service._news_cache   # nada cacheado


async def test_og_image_caido_no_degrada_la_noticia(steam_api, http):
    steam_api.on("GetNewsForApp/v2/", json={"appnews": {"newsitems": [
        {"gid": "1", "title": "A", "url": "https://news/1", "contents": "", "date": 0},
    ]}})
    steam_api.on("news/1", status=500)
    out = await news_service.get_cs2_news(http, 1)
    assert out.status == "ok" and out.data[0]["imageUrl"] == ""
