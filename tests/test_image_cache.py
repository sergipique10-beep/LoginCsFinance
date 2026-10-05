"""CAL-08: el catálogo de imágenes (`catalog_cache`) solo se da por cargado si al menos
una fuente cargó. Si fallan todas, backoff corto (`IMAGE_CATALOG.fail_ttl`) en vez de
23 h sin reintento.

La marca de carga buena es la entrada "catalog" de `catalog_cache.meta` (una TtlCache)
y el fallo total, su `mark_failed`."""
import asyncio
import time
from unittest.mock import MagicMock

import pytest

from steam.cache.image_cache import catalog_cache
from steam.cache.policy import IMAGE_CATALOG
from steam.services import catalog_service

N_SOURCES = 7


class _FakeClient:
    """Cliente httpx simulado: cuenta peticiones y responde con `status` a todas
    salvo a las URLs de `ok_urls`, que reciben una lista válida."""

    def __init__(self, status: int = 500, ok_urls: tuple = ()):
        self.calls = 0
        self._status = status
        self._ok_urls = ok_urls

    async def get(self, url, **kwargs):
        self.calls += 1
        if url in self._ok_urls:
            return MagicMock(status_code=200, json=lambda: [{"name": "AK-47 | Redline", "image": "https://img/x.png"}])
        return MagicMock(status_code=self._status, json=lambda: None)


@pytest.fixture(autouse=True)
def _clean_cache():
    stores = (catalog_cache.images, catalog_cache.rarities, catalog_cache.meta)
    saved = tuple(dict(store) for store in stores)
    catalog_cache.clear()
    yield
    for store, old in zip(stores, saved, strict=True):
        store.clear()
        store.update(old)


async def test_total_failure_does_not_stamp_ts():
    client = _FakeClient(status=500)

    await catalog_service.fetch_static_images(client)

    assert client.calls == N_SOURCES
    assert "catalog" not in catalog_cache.meta


async def test_total_failure_backs_off_then_retries():
    client = _FakeClient(status=500)
    await catalog_service.fetch_static_images(client)

    await catalog_service.fetch_static_images(client)           # dentro del backoff: no pide
    assert client.calls == N_SOURCES

    # Pasado el backoff, la siguiente llamada reintenta las siete descargas.
    catalog_cache.mark_failed(now=time.monotonic() - IMAGE_CATALOG.fail_ttl - 1)
    await catalog_service.fetch_static_images(client)
    assert client.calls == 2 * N_SOURCES


async def test_one_source_ok_stamps_ts():
    client = _FakeClient(status=500, ok_urls=(catalog_service._STATIC_SKINS_URL,))

    await catalog_service.fetch_static_images(client)

    assert "catalog" in catalog_cache.meta
    assert not catalog_cache.meta.in_backoff("catalog")
    assert catalog_cache.images["AK-47 | Redline"] == "https://img/x.png"

    await catalog_service.fetch_static_images(client)           # TTL largo: no vuelve a pedir
    assert client.calls == N_SOURCES


# ── PERF-18: estampida ────────────────────────────────────────────────────────

class _SlowClient(_FakeClient):
    """Cede el control en cada petición para que las corrutinas concurrentes se
    intercalen, como harían con descargas reales."""

    def __init__(self, gate: asyncio.Event | None = None, **kwargs):
        super().__init__(**kwargs)
        self._gate = gate

    async def get(self, url, **kwargs):
        if self._gate is not None:
            await self._gate.wait()
        await asyncio.sleep(0)
        return await super().get(url, **kwargs)


@pytest.fixture(autouse=True)
def _fresh_lock(monkeypatch):
    # Cada test corre en su propio event loop: un Lock que ya esperó en otro loop
    # quedaría ligado a él.
    monkeypatch.setattr(catalog_service, "_image_cache_lock", asyncio.Lock())


async def test_concurrent_reload_downloads_once():
    client = _SlowClient(status=200, ok_urls=(catalog_service._STATIC_SKINS_URL,))

    await asyncio.gather(*[catalog_service.fetch_static_images(client) for _ in range(5)])

    assert client.calls == N_SOURCES          # 7, no 35
    assert "catalog" in catalog_cache.meta


async def test_concurrent_total_failure_downloads_once():
    client = _SlowClient(status=500)

    await asyncio.gather(*[catalog_service.fetch_static_images(client) for _ in range(5)])

    assert client.calls == N_SOURCES          # el backoff también se respeta tras el lock


async def test_cancelled_loader_releases_lock_and_leaves_no_stamp():
    gate = asyncio.Event()
    client = _SlowClient(gate=gate, status=200, ok_urls=(catalog_service._STATIC_SKINS_URL,))

    task = asyncio.create_task(catalog_service.fetch_static_images(client))
    await asyncio.sleep(0)                    # el loader coge el lock y se bloquea en la descarga
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not catalog_service._image_cache_lock.locked()
    assert "catalog" not in catalog_cache.meta
    assert not catalog_cache.meta.in_backoff("catalog")

    gate.set()
    await asyncio.wait_for(catalog_service.fetch_static_images(client), timeout=1)
    assert "catalog" in catalog_cache.meta
