"""CLEAN-09/16: `TtlCache`, la caché con contrato de steam/cache/, y sus políticas.

Sigue siendo un dict de `(valor, ts)` (los tests antiguos lo leen y escriben así), y
centraliza la regla del TTL, el TTL corto de los vacíos, el stale-on-error, el caché
negativo separado del valor (PERF-17) y el tope de entradas.
"""
import re
from pathlib import Path

import stores
from steam.cache import policy
from steam.cache.base_cache import CacheState, TtlCache
from steam.cache.policy import CachePolicy

ROOT = Path(__file__).resolve().parent.parent


def test_fresh_dentro_y_fuera_del_ttl():
    c = TtlCache(10)
    c.put("k", [1], now=100.0)
    assert c.fresh("k", now=109.9) == [1]
    assert c.fresh("k", now=110.0) is None
    assert c.fresh("otra", now=100.0) is None
    assert c["k"] == ([1], 100.0)   # sigue siendo el dict de siempre


def test_un_vacio_fresco_es_un_acierto_y_empty_ttl_lo_acorta():
    c = TtlCache(1000)
    c.put("k", [], now=0.0)
    assert c.fresh("k", now=500.0) == []
    assert c.fresh("k", now=500.0, empty_ttl=300) is None
    assert c.fresh("k", now=299.0, empty_ttl=300) == []
    c.put("lleno", [1], now=0.0)
    assert c.fresh("lleno", now=500.0, empty_ttl=300) == [1]


def test_stale_sirve_el_ultimo_valor_de_cualquier_edad():
    c = TtlCache(10)
    assert c.stale("k") is None
    c.put("k", 0.88, now=0.0)
    assert c.fresh("k", now=1e9) is None
    assert c.stale("k") == 0.88


def test_backoff_separado_del_valor():
    c = TtlCache(10, fail_ttl=300)
    c.put("k", {"a": 1}, now=0.0)
    c.mark_failed("k", now=1000.0)
    assert c.in_backoff("k", now=1299.0)
    assert not c.in_backoff("k", now=1300.0)
    assert c.stale("k") == {"a": 1}            # el fallo no pisa el último dato bueno
    c.mark_failed("k", now=2000.0)
    c.put("k", {"a": 2}, now=2001.0)           # un acierto borra el backoff
    assert not c.in_backoff("k", now=2002.0)


def test_max_entries_expulsa_la_mas_antigua_al_escribir():
    c = TtlCache(10, max_entries=2)
    c.put("a", 1, now=1.0)
    c.put("b", 2, now=2.0)
    c.put("a", 11, now=3.0)                    # reescribir no expulsa
    c.put("c", 3, now=4.0)
    assert set(c) == {"a", "c"}


def test_clear_limpia_tambien_backoff_y_contadores():
    c = TtlCache(10)
    c.put("k", 1, now=0.0)
    c.fresh("k", now=1.0)
    c.mark_failed("k", now=1.0)
    c.clear()
    assert not c and not c.in_backoff("k", now=2.0)
    assert c.stats() == {"entries": 0, "hits": 0, "misses": 0, "stale_served": 0}


def test_contadores():
    c = TtlCache(10)
    c.put("k", 1, now=0.0)
    c.fresh("k", now=1.0)
    c.fresh("k", now=20.0)
    c.fresh("nada", now=1.0)
    c.stale("k")
    c.stale("nada")
    assert c.stats() == {"entries": 1, "hits": 1, "misses": 2, "stale_served": 1}


def test_nadie_compara_cached_1_a_mano():
    # CLEAN-13: antes solo `cached[1]`; un `entry[1] < ttl` se escapaba.
    pattern = re.compile(r"\b\w+\[1\]\s*[<>]|[<>]\s*\w+\[1\]\b|now\s*-\s*\w+\[1\]")
    offenders = [
        str(p.relative_to(ROOT))
        for d in ("steam", "tools")
        for p in (ROOT / d).rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# ── CLEAN-16: política, lookup e invalidación ─────────────────────────────────

def test_from_policy_y_empty_ttl_por_defecto():
    c = TtlCache.from_policy(CachePolicy(1000, empty_ttl=300, fail_ttl=60, max_entries=2), name="x")
    assert (c.ttl, c.empty_ttl, c.fail_ttl, c.max_entries, c.name) == (1000, 300, 60, 2, "x")
    c.put("k", [], now=0.0)
    assert c.fresh("k", now=299.0) == [] and c.fresh("k", now=301.0) is None
    assert c.fresh("k", now=301.0, empty_ttl=1000) == []   # el explícito manda


def test_lookup_devuelve_el_estado():
    c = TtlCache(10, fail_ttl=100)
    assert c.lookup("k", now=0.0) == (CacheState.EMPTY, None)
    c.put("k", 1, now=0.0)
    assert c.lookup("k", now=5.0) == (CacheState.FRESH, 1)
    assert c.lookup("k", now=50.0) == (CacheState.STALE, 1)
    c.mark_failed("k", now=50.0)
    assert c.lookup("k", now=60.0) == (CacheState.ERROR, 1)
    c.mark_failed("nada", now=50.0)
    assert c.lookup("nada", now=60.0) == (CacheState.ERROR, None)


def test_invalidate_y_prefijo():
    c = TtlCache(10, fail_ttl=100)
    for k in ("chat:a", "chat:b", "market:a", 7):
        c.put(k, 1, now=0.0)
    c.mark_failed("chat:a", now=0.0)
    assert c.invalidate_prefix("chat:") == 2
    assert set(c) == {"market:a", 7} and not c.in_backoff("chat:a", now=1.0)
    c.invalidate("market:a")
    c.invalidate("no-existe")
    assert set(c) == {7}


def test_stores_reexporta_los_ttl_de_policy():
    # Hasta la Fase 6 stores.py sigue exponiendo las constantes; su valor sale de policy.
    assert stores.PROFILE_CACHE_TTL == policy.PROFILE.ttl == 82800
    assert stores.HISTORY_EMPTY_TTL == policy.ITEM_HISTORY.empty_ttl == 300
    assert stores.SEARCH_CACHE_TTL == policy.SEARCH.ttl == 300
    assert stores.TtlCache is TtlCache


# ── CLEAN-16 (3.2): instancias por dominio, registro y CatalogCache ───────────

def test_registro_de_caches_y_stats_all():
    import steam.cache as cache
    from steam.cache import history_cache, market_cache, user_cache
    from steam.cache.image_cache import catalog_cache
    assert cache.ALL_CACHES["item_history"] is history_cache._item_history_cache
    assert cache.ALL_CACHES["search"] is market_cache._search_cache
    assert cache.ALL_CACHES["inventory"] is user_cache._inventory_cache
    assert cache.ALL_CACHES["image_catalog"] is catalog_cache
    assert set(cache.stats_all()) == set(cache.ALL_CACHES)
    market_cache._search_cache.put("market:x", [1], now=0.0)
    catalog_cache.register(["AK"], "https://img")
    cache.clear_all()
    assert not market_cache._search_cache and not catalog_cache
    assert all(st["entries"] == 0 for st in cache.stats_all().values())


def test_stores_alias_son_las_mismas_instancias():
    from steam.cache.image_cache import catalog_cache
    from steam.cache.market_cache import _search_cache
    assert stores._search_cache is _search_cache
    assert stores._item_image_cache is catalog_cache.images and stores._image_cache_meta is catalog_cache.meta


def test_catalog_cache():
    from steam.cache.image_cache import CatalogCache
    c = CatalogCache()
    assert not c and not c.is_fresh_or_backoff(now=0.0)
    c.register(["AK-47 | Redline (Field-Tested)", "AK-47 | Redline"], "https://img/ak", ("Classified", "d32ce6"))
    c.register(["Sticker | X"], "https://img/st")
    assert len(c) == 3 and c.image_for(["nada", "AK-47 | Redline"]) == "https://img/ak"
    assert c.image_for(["nada"]) == "" and c.rarity_for("Sticker | X") is None
    assert c.rarity_for("AK-47 | Redline") == ("Classified", "d32ce6")
    c.mark_loaded(now=0.0)
    assert c.is_fresh_or_backoff(now=1.0) and c.stats()["entries"] == 3 and c.stats()["rarities"] == 2
    c.mark_failed(now=10.0)
    assert c.is_fresh_or_backoff(now=20.0) and not c.is_fresh_or_backoff(now=1e9)
    c.clear()
    assert len(c) == 0 and not c.is_fresh_or_backoff(now=20.0)
