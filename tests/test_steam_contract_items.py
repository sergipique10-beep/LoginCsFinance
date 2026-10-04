"""CAL-09 (Fase 0): contrato de /me, /inventory, /inventory/refresh, /item/history y
/news/cs2 con las APIs externas simuladas por HTTP (fixture `steam_api`).

Status, conjunto EXACTO de claves y cabeceras de degradación. Durante STEAM-REFACTOR
estos tests no se tocan. Los `xfail(strict=True)` afirman el comportamiento CORRECTO
de un bug abierto (CAL-13, CAL-14): el día que se arregle, el xfail estricto obliga a
quitar la marca.
"""
import httpx
import pytest

from tests.conftest import SNAPSHOT_DB, STEAM_ID
from tests.test_steam_contract_market import NAME, RAW, SKIN_CARD_KEYS

PROFILE_KEYS = {"userName", "avatarUrl", "avatarThumbUrl", "profileUrl", "isOnline", "steam64_id"}
HISTORY_KEYS = {"date", "price", "volume"}
NEWS_KEYS = {
    "id", "category", "categoryColor", "title", "source", "date", "imageUrl",
    "featured", "url", "content",
}
PROFILE = {"personaname": "Marc", "avatarfull": "https://a/full", "avatarmedium": "https://a/med",
           "profileurl": "https://steamcommunity.com/id/x", "personastate": 1}
NEWS = {"appnews": {"newsitems": [{
    "gid": "1", "title": "Release Notes", "url": "https://news/1", "contents": "Fixed things",
    "date": 1700000000, "author": "Valve", "feedlabel": "Community Announcements",
}]}}
TIMEOUT = httpx.ReadTimeout("timeout")
NET_DOWN = httpx.ConnectError("down")


# ── /me ───────────────────────────────────────────────────────────────────────

def test_me(steam_api, client):
    steam_api.on("api/profile", json=[PROFILE])
    resp = client.get("/me")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == PROFILE_KEYS
    assert body["isOnline"] is True
    assert body["steam64_id"] == STEAM_ID
    assert body["avatarThumbUrl"] == "https://a/med"


def test_me_acepta_objeto_y_cachea(steam_api, client):
    steam_api.on("api/profile", json={**PROFILE, "personastate": 0})
    assert client.get("/me").json()["isOnline"] is False
    assert set(client.get("/me").json()) == PROFILE_KEYS
    assert len(steam_api.hits("api/profile")) == 1


@pytest.mark.parametrize("route, status", [
    ({"exc": TIMEOUT}, 504),
    ({"exc": NET_DOWN}, 502),
    ({"status": 500}, 502),
])
def test_me_errores(steam_api, client, route, status):
    steam_api.on("api/profile", **route)
    assert client.get("/me").status_code == status


@pytest.mark.xfail(strict=True, reason="CAL-14")
def test_me_402_es_503_upstream_quota(steam_api, client):
    steam_api.on("api/profile", status=402)
    resp = client.get("/me")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "upstream_quota"


def test_me_no_cachea_un_perfil_vacio(steam_api, client):
    steam_api.on("api/profile", json=[])
    client.get("/me")
    client.get("/me")
    assert len(steam_api.hits("api/profile")) == 2


# ── /inventory y /inventory/refresh ──────────────────────────────────────────

def test_inventory(steam_api, client):
    steam_api.on("api/inventory", json=[RAW])
    resp = client.get("/inventory")
    assert resp.status_code == 200
    assert set(resp.json()[0]) == SKIN_CARD_KEYS
    assert "x-inventory-stale" not in resp.headers
    assert SNAPSHOT_DB[STEAM_ID][0] == resp.json()


def test_inventory_402_sirve_snapshot_con_cabeceras(steam_api, client):
    SNAPSHOT_DB[STEAM_ID] = ([{"name": NAME}], "2026-10-03T10:00:00+00:00")
    steam_api.on("api/inventory", status=402)
    resp = client.get("/inventory")
    assert resp.status_code == 200
    assert resp.json() == [{"name": NAME}]
    assert resp.headers["X-Inventory-Stale"] == "1"
    assert resp.headers["X-Inventory-Captured-At"] == "2026-10-03T10:00:00+00:00"


def test_inventory_402_sin_snapshot_es_503_upstream_quota(steam_api, client):
    steam_api.on("api/inventory", status=402)
    resp = client.get("/inventory")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "upstream_quota"


@pytest.mark.parametrize("route, status", [
    ({"exc": TIMEOUT}, 504),
    ({"exc": NET_DOWN}, 502),
    ({"status": 403}, 403),
    ({"status": 500}, 502),
    ({"json": {"error": "x"}}, 502),
])
def test_inventory_errores(steam_api, client, route, status):
    steam_api.on("api/inventory", **route)
    assert client.get("/inventory").status_code == status


@pytest.mark.xfail(strict=True, reason="CAL-13")
def test_inventory_410_no_pisa_el_snapshot(steam_api, client):
    SNAPSHOT_DB[STEAM_ID] = ([{"name": NAME}], "2026-10-03T10:00:00+00:00")
    steam_api.on("api/inventory", status=410)
    resp = client.get("/inventory")
    assert resp.headers.get("X-Inventory-Stale") == "1"
    assert SNAPSHOT_DB[STEAM_ID][0] == [{"name": NAME}]


@pytest.mark.xfail(strict=True, reason="CAL-14")
def test_inventory_json_invalido_es_502(steam_api, client):
    steam_api.on("api/inventory", content=b"<html>")
    assert client.get("/inventory").status_code == 502


def test_inventory_refresh(steam_api, client):
    steam_api.on("api/inventory", json=[RAW])
    resp = client.post("/inventory/refresh")
    assert resp.status_code == 200
    assert set(resp.json()[0]) == SKIN_CARD_KEYS
    assert client.post("/inventory/refresh").status_code == 429   # cooldown


# ── /item/history ─────────────────────────────────────────────────────────────

POINTS = [
    {"createdat": "2026-10-02T00:00:00", "price": 21, "sold": 4, "quantity": 7},
    {"createdat": "2026-10-01T00:00:00", "price": "20.5", "sold": 3, "quantity": 6},
    {"createdat": "2026-09-30T00:00:00", "price": None, "sold": 9, "quantity": 9},
]


def test_item_history_steam(steam_api, client):
    steam_api.on("api/history", json=POINTS)
    resp = client.get("/item/history", params={"name": NAME})
    assert resp.status_code == 200
    assert resp.json() == [
        {"date": "2026-10-01", "price": 20.5, "volume": 3},
        {"date": "2026-10-02", "price": 21.0, "volume": 4},
    ]


def test_item_history_por_mercado(steam_api, client):
    steam_api.on("buff/history", json=POINTS)
    resp = client.get("/item/history", params={"name": NAME, "market": "BUFF", "days": 9999})
    assert resp.status_code == 200
    body = resp.json()
    assert all(set(p) == HISTORY_KEYS for p in body)
    assert [p["volume"] for p in body] == [6, 7]   # quantity, no sold
    assert steam_api.hits("buff/history")[0].url.params["market_hash_name"] == NAME


def test_item_history_429_sin_cache_es_503_con_retry_after(steam_api, client):
    steam_api.on("api/history", status=429)
    resp = client.get("/item/history", params={"name": NAME})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "upstream_rate_limit"
    assert resp.headers["Retry-After"] == "60"


@pytest.mark.parametrize("route, status", [
    ({"exc": TIMEOUT}, 504),
    ({"exc": NET_DOWN}, 502),
    ({"status": 500}, 502),
])
def test_item_history_errores(steam_api, client, route, status):
    steam_api.on("api/history", **route)
    assert client.get("/item/history", params={"name": NAME}).status_code == status


@pytest.mark.xfail(strict=True, reason="CAL-14")
def test_item_history_402_sin_cache_es_503_upstream_quota(steam_api, client):
    steam_api.on("api/history", status=402)
    resp = client.get("/item/history", params={"name": NAME})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "upstream_quota"


def test_item_history_no_cachea_23h_un_cuerpo_que_no_es_lista(steam_api, client):
    steam_api.on("api/history", json={"error": "x"})
    client.get("/item/history", params={"name": NAME})
    client.get("/item/history", params={"name": NAME})
    assert len(steam_api.hits("api/history")) == 2


# ── /news/cs2 ─────────────────────────────────────────────────────────────────

def test_news(steam_api, client):
    steam_api.on("GetNewsForApp/v2/", json=NEWS)
    resp = client.get("/news/cs2")
    assert resp.status_code == 200
    (item,) = resp.json()
    assert set(item) == NEWS_KEYS
    assert item["featured"] is True
    assert item["imageUrl"] == ""   # og:image caído → cadena vacía


def test_news_sin_appnews_es_lista_vacia(steam_api, client):
    steam_api.on("GetNewsForApp/v2/", json={})
    assert client.get("/news/cs2").json() == []


@pytest.mark.parametrize("route, status", [
    ({"exc": TIMEOUT}, 504),
    ({"exc": NET_DOWN}, 502),
    ({"status": 500}, 502),
])
def test_news_errores(steam_api, client, route, status):
    steam_api.on("GetNewsForApp/v2/", **route)
    assert client.get("/news/cs2").status_code == status


@pytest.mark.xfail(strict=True, reason="CAL-14")
def test_news_json_que_no_es_objeto_es_502(steam_api, client):
    steam_api.on("GetNewsForApp/v2/", json=[])
    assert client.get("/news/cs2").status_code == 502
