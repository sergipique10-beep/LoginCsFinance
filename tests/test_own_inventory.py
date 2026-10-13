"""COST-07 · Lado Render: inventario y perfil desde el worker (own_inventory_service, own_source_repo).

Por defecto (INVENTORY_SOURCE=steamwebapi) nada de esto se ejecuta: el último test lo fija.
"""
import datetime as dt
from types import SimpleNamespace

import pytest

import settings
from steam import own_source_repo as repo
from steam.cache.user_cache import _profile_cache
from steam.errors import UpstreamError
from steam.services import inventory_service, own_inventory_service as own, profile_service
from tests.conftest import STEAM_ID

NOW_ISO = dt.datetime.now(dt.UTC).isoformat()
OLD_ISO = (dt.datetime.now(dt.UTC) - dt.timedelta(days=3)).isoformat()

ASSET = {"assetid": "111", "classid": "c", "instanceid": "0", "amount": 1,
         "market_hash_name": "★ Karambit | Doppler (Factory New)", "name": "★ Karambit | Doppler",
         "icon_url": "abc", "item_type": "Knife", "exterior": "Factory New", "rarity": "Covert",
         "rarity_color": "eb4b4b", "quality": "★", "is_stattrak": False, "is_souvenir": False, "is_star": True,
         "tradable": True, "marketable": True, "float_value": 0.0123, "paint_seed": 661, "paint_index": 418,
         "phase": "Phase 1"}
MARKET = {"market_hash_name": ASSET["market_hash_name"], "ref_price_usd": "1200.5", "ref_confidence": 2,
          "volume_24h": 3, "prices": [
              {"market": "buff", "price": 1150.0, "volume": None, "listings": 40, "used": True},
              {"market": "skinport", "price": 1250.0, "volume": None, "listings": 5, "used": True},
              {"market": "marketcsgo", "price": 4000.0, "volume": None, "listings": 1, "used": False}]}


class FakeRepo:
    def __init__(self, snap=None, states=(("done", None),), after=None, profile=None):
        self.snap, self.after, self.profile = snap, after, profile
        self.states = list(states)
        self.requests = []

    def install(self, mp):
        async def request(kind, steam_id, prio):
            self.requests.append((kind, steam_id, prio))
            return len(self.requests)

        async def request_state(rid):
            st = self.states.pop(0) if self.states else ("pending", None)
            if st[0] == "done" and self.after is not None:
                self.snap = self.after
            return st

        async def load_snapshot(steam_id):
            return self.snap

        async def load_profile(steam_id):
            return self.profile

        async def market_rows(names):
            return {MARKET["market_hash_name"]: MARKET} if MARKET["market_hash_name"] in names else {}
        for n, f in dict(request=request, request_state=request_state, load_snapshot=load_snapshot,
                         load_profile=load_profile, market_rows=market_rows).items():
            mp.setattr(repo, n, f)
        mp.setattr(own, "POLL_S", 0)
        return self


@pytest.fixture(autouse=True)
def _clean():
    own._inventory_cache.clear()
    _profile_cache.clear()


async def test_compone_la_tarjeta_con_el_contrato_de_siempre(monkeypatch):
    FakeRepo().install(monkeypatch)
    (card,) = await own.compose([ASSET])
    from steam.domain.models import SkinCard
    assert set(SkinCard.__annotations__) <= set(card)
    assert card["id"] == "111" and card["name"] == ASSET["market_hash_name"]
    assert (card["floatValue"], card["paintIndex"], card["phase"]) == (0.0123, 418, "Phase 1")
    assert card["priceLatest"] == 1200.5 and card["priceMin"] == 1150.0 and card["priceMax"] == 1250.0
    assert card["sold24h"] == 3 and card["offerVolume"] == 46
    assert {p["market"] for p in card["externalPrices"]} == {"buff", "skinport", "marketcsgo"}
    assert card["image"].endswith("/economy/image/abc") and card["isStar"]
    (bare,) = await own.compose([{**ASSET, "market_hash_name": "Sin precio", "icon_url": None, "paint_index": None}])
    assert bare["priceLatest"] == 0 and bare["externalPrices"] == [] and bare["phase"] is None


async def test_snapshot_fresco_sin_pedir_nada(monkeypatch):
    fr = FakeRepo(snap=([ASSET], NOW_ISO)).install(monkeypatch)
    got = await own.get_inventory(STEAM_ID)
    assert got.status == "ok" and len(got.data.items) == 1 and fr.requests == []
    assert (await own.get_inventory(STEAM_ID)).status == "ok"        # segunda: caché


async def test_snapshot_viejo_pide_y_espera_el_nuevo(monkeypatch):
    fr = FakeRepo(snap=([], OLD_ISO), states=[("queued", None), ("done", None)],
                  after=([ASSET, {**ASSET, "assetid": "222"}], NOW_ISO)).install(monkeypatch)
    got = await own.get_inventory(STEAM_ID)
    assert got.status == "ok" and len(got.data.items) == 2
    assert fr.requests == [("inventory", STEAM_ID, repo.PRIO_INTERACTIVE)]


async def test_force_ignora_el_snapshot_fresco(monkeypatch):
    fr = FakeRepo(snap=([ASSET], NOW_ISO), after=([ASSET], NOW_ISO)).install(monkeypatch)
    assert (await own.get_inventory(STEAM_ID, force=True)).status == "ok" and len(fr.requests) == 1


async def test_privado_es_403(monkeypatch):
    FakeRepo(states=[("error", "private")]).install(monkeypatch)
    with pytest.raises(UpstreamError) as e:
        await own.get_inventory(STEAM_ID)
    assert e.value.status == 403


async def test_timeout_con_snapshot_viejo_es_stale(monkeypatch):
    FakeRepo(snap=([ASSET], OLD_ISO), states=[]).install(monkeypatch)
    monkeypatch.setattr(own, "INVENTORY_WAIT_S", 0.01)
    got = await own.get_inventory(STEAM_ID)
    assert got.status == "stale" and got.reason == "refresh_pending" and got.data.captured_at == OLD_ISO


async def test_sin_snapshot_y_fallo(monkeypatch):
    FakeRepo(states=[("error", "rate_limit")]).install(monkeypatch)
    got = await own.get_inventory(STEAM_ID)
    assert (got.status, got.reason, got.data.items) == ("error", "rate_limit", [])
    FakeRepo(states=[("error", "http_404")]).install(monkeypatch)
    assert (await own.get_inventory(STEAM_ID)).reason == "http_404"


def test_ruta_inventory_en_modo_own(client, monkeypatch):
    monkeypatch.setattr(settings, "INVENTORY_SOURCE", "own")
    FakeRepo(snap=([ASSET], OLD_ISO), states=[("error", "rate_limit")]).install(monkeypatch)
    r = client.get("/inventory")
    assert r.status_code == 200 and r.headers["X-Inventory-Stale"] == "1" and len(r.json()) == 1
    FakeRepo(states=[("error", "private")]).install(monkeypatch)
    assert client.get("/inventory").status_code == 403
    FakeRepo(states=[("error", "rate_limit")]).install(monkeypatch)
    assert client.get("/inventory").status_code == 429


async def test_modo_shadow_sirve_lo_de_siempre_y_encola(monkeypatch):
    monkeypatch.setattr(settings, "INVENTORY_SOURCE", "shadow")
    fr = FakeRepo().install(monkeypatch)

    async def fresh(client, steam_id):
        from steam.domain.models import Fetched
        return Fetched([{"id": "x"}])
    monkeypatch.setattr(inventory_service, "_fetch_fresh_inventory", fresh)

    async def store(*a):
        return None
    monkeypatch.setattr(inventory_service, "_store", store)
    got = await inventory_service.get_inventory(None, STEAM_ID)
    assert got.data.items == [{"id": "x"}] and fr.requests == [("inventory", STEAM_ID, repo.PRIO_BACKGROUND)]


async def test_shadow_nunca_rompe_la_ruta(monkeypatch):
    async def boom(*a):
        raise RuntimeError("supabase caído")
    monkeypatch.setattr(repo, "request", boom)
    await own.enqueue_shadow(STEAM_ID)


async def test_perfil_own(monkeypatch):
    monkeypatch.setattr(settings, "PROFILE_SOURCE", "own")
    row = {"steam_id": STEAM_ID, "persona_name": "Marc", "avatar_full": "f", "avatar_medium": "m",
           "profile_url": "u", "persona_state": 1, "updated_at": NOW_ISO}
    fr = FakeRepo(profile=row).install(monkeypatch)
    got = await profile_service.get_profile(None, STEAM_ID)
    assert got.data["userName"] == "Marc" and got.data["isOnline"] and fr.requests == []
    _profile_cache.clear()
    fr = FakeRepo(profile=None, states=[("error", "upstream")]).install(monkeypatch)
    got = await profile_service.get_profile(None, STEAM_ID)
    assert got.status == "error" and fr.requests == [("profile", STEAM_ID, 0)]


async def test_perfil_shadow_encola(monkeypatch):
    monkeypatch.setattr(settings, "PROFILE_SOURCE", "shadow")
    fr = FakeRepo().install(monkeypatch)

    async def prof(client, steam_id):
        return {"response": {"players": [{"personaname": "x"}]}}
    monkeypatch.setattr(profile_service.steam_client, "profile", prof)
    await profile_service.get_profile(None, STEAM_ID)
    assert fr.requests == [("profile", STEAM_ID, repo.PRIO_BACKGROUND)]


def test_delete_me_borra_lo_del_worker_solo_con_el_modo_activo(client, monkeypatch):
    calls = []

    async def delete(steam_id):
        calls.append(steam_id)

    async def noop(steam_id):
        return None
    import auth.router as auth_router
    monkeypatch.setattr(repo, "delete_for_user", delete)
    monkeypatch.setattr(auth_router.notifications_repo, "delete_device_tokens_for", noop)
    monkeypatch.setattr(auth_router.alerts_repo, "delete_all_for_user", noop)
    monkeypatch.setattr(auth_router.portfolio_repo, "delete_all_for_user", noop)
    assert client.delete("/me").status_code == 200 and calls == []
    monkeypatch.setattr(settings, "INVENTORY_SOURCE", "shadow")
    assert client.delete("/me").status_code == 200 and calls == [STEAM_ID]


class FakeSB:
    def __init__(self, data):
        self.data, self.calls = data, []

    def table(self, name):
        self.calls.append(("table", name))
        return self

    def __getattr__(self, name):
        def f(*a, **k):
            self.calls.append((name, a))
            return self
        return f

    def execute(self):
        return SimpleNamespace(data=self.data)


async def test_repo_contra_supabase_falso(monkeypatch):
    sb = FakeSB([{"id": 7, "status": "done", "error": None, "items": [1], "captured_at": "t",
                  "market_hash_name": "AK", "steam_id": "1"}])
    monkeypatch.setattr(repo, "get_supabase", lambda: sb)
    assert await repo.request("inventory", "1", 0) == 7
    assert await repo.request_state(7) == ("done", None)
    assert await repo.load_snapshot("1") == ([1], "t")
    assert (await repo.load_profile("1"))["id"] == 7
    assert list(await repo.market_rows(["AK"])) == ["AK"] and await repo.market_rows([]) == {}
    await repo.delete_for_user("1")
    tables = [c[1] for c in sb.calls if c[0] == "table"]
    assert {"fetch_requests", "steam_profiles", settings.OWN_INVENTORY_TABLE} <= set(tables)
    empty = FakeSB([])
    monkeypatch.setattr(repo, "get_supabase", lambda: empty)
    assert await repo.load_snapshot("1") is None and await repo.request_state(1) == ("pending", None)


def test_por_defecto_steamwebapi():
    assert settings.INVENTORY_SOURCE == "steamwebapi" and settings.PROFILE_SOURCE == "steamwebapi"
    assert own._age_h("no-es-fecha") == float("inf") and own._age_h(NOW_ISO.replace("+00:00", "")) < 1
