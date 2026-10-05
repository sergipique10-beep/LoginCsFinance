"""CLEAN-19 (6.1): steam/utils/ — strings, dates y urls. Sin dependencias internas (guardia
de capas en tests/test_steam_layers.py)."""
from datetime import date, datetime, timezone

from steam.mappers import news_mapper
from steam.utils import dates
from steam.utils.dates import hour_floor, iso_day
from steam.utils.strings import clean_news_content, lower_key
from steam.utils.urls import STEAM_CDN, is_http_url, steam_cdn_url


def test_clean_news_content_quita_markup_y_acota():
    raw = "<b>Hola</b> [url=x]mundo[/url] {STEAM_CLAN_IMAGE}/a.png https://x.y/z &amp; fin"
    assert clean_news_content(raw) == "Hola mundo & fin"
    assert clean_news_content("una dos tres cuatro", max_chars=9) == "una dos"
    # rag/ y notifications/ siguen importando el nombre antiguo del mapper
    assert news_mapper._clean_news_content is clean_news_content


def test_lower_key():
    assert lower_key("AK-47 | Redline") == "ak-47 | redline"
    assert lower_key(None) == "" and lower_key("") == ""


def test_iso_day_y_hour_floor():
    assert iso_day("2026-10-05T07:42:11Z") == "2026-10-05"
    assert iso_day(None) == "" and iso_day("2026") == "2026"
    moment = datetime(2026, 10, 5, 7, 42, 11, 123, tzinfo=timezone.utc)
    assert hour_floor(moment) == datetime(2026, 10, 5, 7, tzinfo=timezone.utc)


def test_today_es_inyectable(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: date(2026, 1, 1))
    assert dates.today() == date(2026, 1, 1)


def test_urls():
    assert is_http_url("https://a") and not is_http_url("economy/x")
    assert steam_cdn_url("abc") == f"{STEAM_CDN}/economy/image/abc"
