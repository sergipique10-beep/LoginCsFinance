"""CLEAN-11: los services pequeños de steam/services/ (inventario, perfil y noticias),
llamados directamente con steamwebapi y Steam News simulados por HTTP."""
from unittest.mock import AsyncMock

import pytest

from steam.errors import UnexpectedPayload, UpstreamError
from steam.services import inventory as inventory_service
from steam.services import news as news_service
from steam.services import profile as profile_service
from tests.test_steam_contract_market import NAME, RAW


@pytest.fixture
def fake(steam_api):
    return steam_api


@pytest.fixture
def http(fake):
    import main
    return main.app.state.http_client


@pytest.fixture
def register(monkeypatch):
    reg = AsyncMock()
    monkeypatch.setattr("steam.price_history_repo.register_tracked", reg)
    return reg


# ── inventario ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("track, registered", [(True, True), (False, False)])
async def test_inventario_track_decide_si_se_registra(fake, http, register, track, registered):
    fake.on("api/inventory", json=[RAW])
    items = await inventory_service.fetch_fresh_inventory(http, "1", track=track)
    assert [i["name"] for i in items] == [NAME]
    assert register.await_count == int(registered)
    if registered:
        register.assert_awaited_once_with([NAME], "inventory")


async def test_inventario_que_no_es_lista(fake, http, register):
    fake.on("api/inventory", json={"error": "x"})
    with pytest.raises(UnexpectedPayload):
        await inventory_service.fetch_fresh_inventory(http, "1", track=True)


async def test_inventario_410_lo_decide_quien_llama(fake, http, register):
    # La ruta lo convierte en [] y el chat en un fallo (CAL-13): el service no se lo traga.
    fake.on("api/inventory", status=410)
    with pytest.raises(UpstreamError) as info:
        await inventory_service.fetch_fresh_inventory(http, "1", track=True)
    assert info.value.status == 410


# ── perfil ────────────────────────────────────────────────────────────────────

async def test_perfil_lista_vacia_y_cache(fake, http):
    fake.on("api/profile", json=[])
    profile = await profile_service.get_profile(http, "765")
    assert profile["steam64_id"] == "765" and profile["userName"] == "" and profile["isOnline"] is False
    assert await profile_service.get_profile(http, "765") == profile
    assert len(fake.hits("api/profile")) == 1


# ── noticias ──────────────────────────────────────────────────────────────────

def _news(*titles):
    return {"appnews": {"newsitems": [
        {"gid": str(i), "title": t, "url": "", "contents": "", "date": 0} for i, t in enumerate(titles)
    ]}}


async def test_noticias_piden_de_mas_y_filtran_alfabetos(fake, http):
    fake.on("GetNewsForApp/v2/", json=_news("Обновление", "Release Notes", "更新", "Patch"))
    out = await news_service.get_cs2_news(http, 2)
    assert [n["title"] for n in out] == ["Release Notes", "Patch"]
    assert fake.hits("GetNewsForApp/v2/")[0].url.params["count"] == "6"   # 2 × NEWS_OVERFETCH


async def test_noticias_tope_y_sin_legibles_se_quedan_las_originales(fake, http):
    fake.on("GetNewsForApp/v2/", json=_news("Обновление"))
    out = await news_service.get_cs2_news(http, 50)
    assert [n["title"] for n in out] == ["Обновление"]
    assert fake.hits("GetNewsForApp/v2/")[0].url.params["count"] == str(news_service.NEWS_MAX_FETCH)
