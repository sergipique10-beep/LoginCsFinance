"""Cliente de frankfurter: tipos de referencia del BCE, sin clave. La
validación del rango y la caché con stale viven en `services/fx.fetch_fx_rate`."""
from typing import Any

import httpx

from steam.api.http import get_json

# El host .app redirige 301 a .dev, asi que se apunta directo a .dev.
_FX_API = "https://api.frankfurter.dev/v1/latest"
_TIMEOUT = 10.0


async def latest_usd_eur(client: httpx.AsyncClient) -> Any:
    return await get_json(client, _FX_API, {"base": "USD", "symbols": "EUR"}, timeout=_TIMEOUT)
