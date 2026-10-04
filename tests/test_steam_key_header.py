"""SEC-13: la clave de steamwebapi viaja en la cabecera X-Api-Key, nunca en la URL,
así que no puede acabar en los logs (INFO de httpx, str() de sus excepciones)."""
import logging
import re
from pathlib import Path

import httpx
import pytest

from steam import price_capture
from steam.services import pricing, providers
from steam.api import steam_client
from steam.errors import UpstreamError
from stores import _item_history_cache, _market_lookup_cache, _market_providers_cache

FAKE_KEY = "test-sentinel-not-a-real-key-0001"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(steam_client, "STEAM_API_KEY", FAKE_KEY)
    stores = (_item_history_cache, _market_lookup_cache, _market_providers_cache)
    for s in stores:
        s.clear()
    steam_client._history_limiter._calls = []
    yield
    for s in stores:
        s.clear()
    steam_client._history_limiter._calls = []


def _client(seen: list, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=[], request=request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_key_goes_in_header_and_never_reaches_logs(caplog):
    caplog.set_level(logging.DEBUG)                     # peor caso: todo el logging a DEBUG
    seen: list[httpx.Request] = []
    async with _client(seen) as client:
        await pricing.fetch_history_for_item(client, "AK-47 | Redline (Field-Tested)")
        await pricing._fetch_market_price_lookup(client, "csfloat")
        await providers.fetch_market_providers(client)
        await price_capture._lookup_item(client, "AK-47 | Redline (Field-Tested)")

    assert len(seen) == 4
    for req in seen:
        assert req.headers["X-Api-Key"] == FAKE_KEY
        assert FAKE_KEY not in str(req.url)
        assert "key" not in req.url.params
    assert FAKE_KEY not in caplog.text


async def test_http_error_with_url_does_not_leak_key(caplog):
    # El error de un status no 200 se registra entero en los logs (price_capture, alertas).
    caplog.set_level(logging.DEBUG)
    async with _client([], status=500) as client:
        with pytest.raises(UpstreamError) as exc_info:
            await price_capture._lookup_item(client, "X")
    logging.getLogger("uvicorn.error").warning("[test] %s", exc_info.value)
    assert FAKE_KEY not in caplog.text


def test_no_steamwebapi_call_puts_key_in_query():
    # Guardia estática para las llamadas de routes/ y tools/ que no se ejercitan arriba.
    pattern = re.compile(r"""["']key["']\s*:\s*STEAM_API_KEY""")
    offenders = [
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("*.py")
        if not {"venv", "tests", ".git"} & set(p.relative_to(ROOT).parts)
        and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_solo_el_cliente_conoce_las_urls_de_steamwebapi():
    # CLEAN-06: toda llamada a steamwebapi pasa por steam/api/steam_client.py, que es
    # el único sitio que pone la clave. Una URL fuera de él sería una llamada sin cliente.
    pattern = re.compile(r"STEAM_WEB_API|STEAM_MARKET_API|steamwebapi\.com")
    own = ROOT / "steam" / "api" / "steam_client.py"
    offenders = [
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("*.py")
        if p != own
        and not {"venv", "tests", ".git"} & set(p.relative_to(ROOT).parts)
        and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
