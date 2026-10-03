"""SEC-09 — proxy de la API pública de Leetify.

La clave vive solo aquí (LEETIFY_API_KEY). Antes la incrustaba el bundle del
frontend (`NG_APP_LEETIFY_API_KEY`), legible en el JS de la web y en el APK.

El SteamID sale SIEMPRE del `sub` del JWT, nunca de un parámetro: el endpoint
solo sirve las estadísticas del propio usuario. Un `?steam64_id=` que mande el
cliente se ignora.
"""
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from auth.service import leetify_rate_limit, require_jwt
from settings import LEETIFY_API_KEY
from stores import LEETIFY_CACHE_TTL, _leetify_cache

logger = logging.getLogger("uvicorn.error")

LEETIFY_API = "https://api-public.cs-prod.leetify.com"

router = APIRouter(dependencies=[Depends(leetify_rate_limit)])


async def _leetify_get(request: Request, path: str, steam_id: str):
    """GET cacheado (5 min por usuario y ruta) a Leetify. Los errores no se cachean."""
    if not LEETIFY_API_KEY:
        raise HTTPException(status_code=503, detail="Leetify not configured")

    key = (steam_id, path)
    now = time.monotonic()
    cached = _leetify_cache.get(key)
    if cached and now - cached[1] < LEETIFY_CACHE_TTL:
        return cached[0]

    try:
        resp = await request.app.state.http_client.get(
            f"{LEETIFY_API}{path}",
            params={"steam64_id": steam_id},
            headers={"Authorization": f"Bearer {LEETIFY_API_KEY}"},
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Leetify request timed out")
    except httpx.RequestError as exc:
        logger.error("[stats] leetify unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Could not reach Leetify")

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="No Leetify profile")
    if resp.status_code != 200:
        # 401/403 = clave caducada o rotada mal: es nuestro fallo, no del usuario.
        logger.error("[stats] leetify %s → %s: %.300s", path, resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail=f"Leetify returned {resp.status_code}")

    data = resp.json()
    _leetify_cache[key] = (data, now)
    return data


@router.get("/me/stats", summary="Perfil de Leetify del usuario autenticado")
async def get_my_stats(request: Request, user: dict = Depends(require_jwt)):
    return await _leetify_get(request, "/v3/profile", user["sub"])


@router.get("/me/stats/matches", summary="Últimas partidas de Leetify del usuario autenticado")
async def get_my_matches(request: Request, user: dict = Depends(require_jwt)):
    return await _leetify_get(request, "/v3/profile/matches", user["sub"])
