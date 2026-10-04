"""CAL-08: la caché de imágenes solo se da por cargada si al menos una fuente cargó.
Si fallan todas, backoff corto (_IMAGE_EMPTY_TTL) en vez de 23 h sin reintento."""
import asyncio
import time
from unittest.mock import MagicMock

import pytest

from steam import services
from stores import _image_cache_meta, _item_image_cache, _item_rarity_cache

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
    saved = (dict(_item_image_cache), dict(_item_rarity_cache), dict(_image_cache_meta))
    for store in (_item_image_cache, _item_rarity_cache, _image_cache_meta):
        store.clear()
    yield
    for store, old in zip((_item_image_cache, _item_rarity_cache, _image_cache_meta), saved, strict=True):
        store.clear()
        store.update(old)


async def test_total_failure_does_not_stamp_ts():
    client = _FakeClient(status=500)

    await services._fetch_static_images(client)

    assert client.calls == N_SOURCES
    assert "ts" not in _image_cache_meta


async def test_total_failure_backs_off_then_retries():
    client = _FakeClient(status=500)
    await services._fetch_static_images(client)

    await services._fetch_static_images(client)           # dentro del backoff: no pide
    assert client.calls == N_SOURCES

    # Pasado el backoff, la siguiente llamada reintenta las siete descargas.
    _image_cache_meta["failed_ts"] = time.monotonic() - services._IMAGE_EMPTY_TTL - 1
    await services._fetch_static_images(client)
    assert client.calls == 2 * N_SOURCES


async def test_one_source_ok_stamps_ts():
    client = _FakeClient(status=500, ok_urls=(services._STATIC_SKINS_URL,))

    await services._fetch_static_images(client)

    assert "ts" in _image_cache_meta
    assert "failed_ts" not in _image_cache_meta
    assert _item_image_cache["AK-47 | Redline"] == "https://img/x.png"

    await services._fetch_static_images(client)           # TTL largo: no vuelve a pedir
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
    monkeypatch.setattr(services, "_image_cache_lock", asyncio.Lock())


async def test_concurrent_reload_downloads_once():
    client = _SlowClient(status=200, ok_urls=(services._STATIC_SKINS_URL,))

    await asyncio.gather(*[services._fetch_static_images(client) for _ in range(5)])

    assert client.calls == N_SOURCES          # 7, no 35
    assert "ts" in _image_cache_meta


async def test_concurrent_total_failure_downloads_once():
    client = _SlowClient(status=500)

    await asyncio.gather(*[services._fetch_static_images(client) for _ in range(5)])

    assert client.calls == N_SOURCES          # el backoff también se respeta tras el lock


async def test_cancelled_loader_releases_lock_and_leaves_no_stamp():
    gate = asyncio.Event()
    client = _SlowClient(gate=gate, status=200, ok_urls=(services._STATIC_SKINS_URL,))

    task = asyncio.create_task(services._fetch_static_images(client))
    await asyncio.sleep(0)                    # el loader coge el lock y se bloquea en la descarga
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not services._image_cache_lock.locked()
    assert "ts" not in _image_cache_meta
    assert "failed_ts" not in _image_cache_meta

    gate.set()
    await asyncio.wait_for(services._fetch_static_images(client), timeout=1)
    assert "ts" in _image_cache_meta
