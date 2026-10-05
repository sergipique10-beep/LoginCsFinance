"""CLEAN-11: los services pequeños de steam/services/ (inventario, perfil y noticias),
llamados directamente con steamwebapi y Steam News simulados por HTTP."""
from unittest.mock import AsyncMock

import pytest

from steam.domain.models import Fetched
from steam.errors import UnexpectedPayload
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
    fetched = await inventory_service.fetch_fresh_inventory(http, "1", track=track)
    assert fetched.status == "ok" and [i["name"] for i in fetched.data] == [NAME]
    assert register.await_count == int(registered)
    if registered:
        register.assert_awaited_once_with([NAME], "inventory")


async def test_inventario_que_no_es_lista(fake, http, register):
    fake.on("api/inventory", json={"error": "x"})
    with pytest.raises(UnexpectedPayload):
        await inventory_service.fetch_fresh_inventory(http, "1", track=True)


@pytest.mark.parametrize("status", [410, 411])
async def test_inventario_410_es_error_sin_datos(fake, http, register, status):
    # CAL-13: no hay inventario que leer. La ruta decide (snapshot o []) y nadie guarda el vacío.
    fake.on("api/inventory", status=status)
    fetched = await inventory_service.fetch_fresh_inventory(http, "1", track=True)
    assert fetched == Fetched([], "error", f"http_{status}")
    register.assert_not_awaited()


# ── perfil ────────────────────────────────────────────────────────────────────

async def test_perfil_lista_vacia_y_cache(fake, http):
    fake.on("api/profile", json=[])
    profile = await profile_service.get_profile(http, "765")
    assert profile["steam64_id"] == "765" and profile["userName"] == "" and profile["isOnline"] is False
    # CAL-14 (CLEAN-14): un 200 sin perfil no se cachea, así que la segunda llamada vuelve a pedirlo.
    assert await profile_service.get_profile(http, "765") == profile
    assert len(fake.hits("api/profile")) == 2


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
