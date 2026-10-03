import re
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

import jwt
from settings import (
    ALLOWED_REDIRECT_ORIGINS,
    BASE_URL,
    COOKIE_SECURE,
    DEV_TOKEN_ENABLED,
    FRONTEND_URL,
    JWT_SECRET,
    REVIEW_PASSWORD,
    REVIEW_STEAM_ID,
    REVIEW_USER,
)
from stores import (
    _auth_codes, CODE_TTL, TOKEN_AUDIENCE,
    _profile_cache, _inventory_cache, _inventory_refresh_cooldown,
)
from auth.service import (
    _consume_nonce,
    _get_client_ip,
    _issue_nonce,
    _issue_tokens,
    _rate_limit,
    _refresh_token_from_body,
    _token_response,
    require_jwt,
    session_store,
)
from auth import refresh_repo
from notifications import repo as notifications_repo
from alerts import repo as alerts_repo
from portfolio import repo as portfolio_repo
from steam import inventory_snapshot_repo

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"

router = APIRouter()


@router.get("/auth/steam", summary="Redirige al login de Steam")
def steam_login(request: Request, platform: str = "web"):
    _rate_limit(_get_client_ip(request))

    if platform == "android":
        redirect_origin = next(
            (o for o in ALLOWED_REDIRECT_ORIGINS if o.startswith("myapp://")),
            None,
        )
        if redirect_origin is None:
            raise HTTPException(status_code=400, detail="Android redirect origin not configured")
    else:
        redirect_origin = FRONTEND_URL

    if redirect_origin not in ALLOWED_REDIRECT_ORIGINS:
        raise HTTPException(status_code=400, detail="Redirect origin not allowed")

    nonce = _issue_nonce(redirect_origin)
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": f"{BASE_URL}/auth/steam/callback?nonce={nonce}",
        "openid.realm": BASE_URL,
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    return RedirectResponse(url=f"{STEAM_OPENID_URL}?{urlencode(params)}")


@router.get("/auth/steam/callback", summary="Callback OpenID de Steam — emite auth code")
async def steam_callback(request: Request, nonce: str = ""):
    # 1. CSRF: verify nonce and recover the redirect_origin sealed at flow start
    redirect_origin = _consume_nonce(nonce) if nonce else None
    if redirect_origin is None:
        raise HTTPException(status_code=400, detail="Invalid or expired nonce")

    query_params = dict(request.query_params)

    # 2. Replay: return_to must point to our own callback
    return_to = query_params.get("openid.return_to", "")
    if not return_to.startswith(f"{BASE_URL}/auth/steam/callback"):
        raise HTTPException(status_code=400, detail="Tampered return_to URL")

    # 3. Verify with Steam
    validation_params = {**query_params, "openid.mode": "check_authentication"}
    try:
        resp = await request.app.state.http_client.post(STEAM_OPENID_URL, data=validation_params)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Steam validation timed out")
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Could not reach Steam servers")

    if "is_valid:true" not in resp.text:
        raise HTTPException(status_code=401, detail="Steam authentication failed")

    # 4. Extract and validate Steam ID (exactly 17 digits)
    claimed_id = query_params.get("openid.claimed_id", "")
    match = re.search(r"/openid/id/(\d{17})$", claimed_id)
    if not match:
        raise HTTPException(status_code=400, detail="Could not parse Steam ID")

    steam_id = match.group(1)

    # 5. Issue one-time auth code (TTL CODE_TTL seconds)
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = (steam_id, time.monotonic() + CODE_TTL)

    return RedirectResponse(url=f"{redirect_origin}/auth/callback?code={code}")


@router.post("/auth/token", summary="Canjea el auth code por access token + refresh (cookie en web, cuerpo en nativo)")
async def exchange_token(request: Request):
    _rate_limit(_get_client_ip(request))

    body = await request.json()
    code: str = body.get("code", "")

    if not code:
        raise HTTPException(status_code=400, detail="Missing code")

    # Consume the code (atomic: read + delete)
    entry = _auth_codes.pop(code, None)
    if entry is None:
        raise HTTPException(status_code=400, detail="Invalid or already used code")

    steam_id, expires_at = entry
    if time.monotonic() > expires_at:
        raise HTTPException(status_code=400, detail="Code expired")

    access_token, refresh_token = await _issue_tokens(steam_id)

    return _token_response(request, access_token, refresh_token)


@router.post("/auth/dev-token", summary="[DEV ONLY] Emite tokens para un steam_id sin pasar por Steam OpenID")
async def dev_token(request: Request):
    # Exige DEBUG=true Y ENV!=production (SEC-02): un DEBUG colado en Render no
    # basta para revivir el endpoint. 404 en cualquier otro caso.
    if not DEV_TOKEN_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    body = await request.json()
    steam_id: str = body.get("steam_id", "")

    if not re.match(r"^\d{17}$", steam_id):
        raise HTTPException(status_code=400, detail="steam_id must be exactly 17 digits")

    access_token, refresh_token = await _issue_tokens(steam_id)
    return _token_response(request, access_token, refresh_token)


def _eq(a: str, b: str) -> bool:
    """Comparación de tiempo constante que admite cualquier str (SEC-04)."""
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@router.post("/auth/review-login", summary="Acceso de revisión (Google Play) sin Steam")
async def review_login(request: Request):
    _rate_limit(_get_client_ip(request))
    if not (REVIEW_USER and REVIEW_PASSWORD and REVIEW_STEAM_ID):
        raise HTTPException(status_code=404, detail="Not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")
    # compare_digest sobre `str` sólo admite ASCII: un acento lanzaba TypeError
    # → 500 alcanzable pre-auth por cualquier anónimo (SEC-04). Sobre `bytes` no
    # tiene esa restricción y sigue siendo de tiempo constante, así que una
    # credencial no-ASCII acaba en 401, que es la respuesta correcta.
    user = str(body.get("user", ""))
    password = str(body.get("password", ""))
    if not (_eq(user, REVIEW_USER) and _eq(password, REVIEW_PASSWORD)):
        raise HTTPException(status_code=401, detail="Invalid review credentials")

    access_token, refresh_token = await _issue_tokens(REVIEW_STEAM_ID)
    return _token_response(request, access_token, refresh_token)


@router.post("/auth/refresh", summary="Rota el refresh token y devuelve nuevo access token")
async def refresh_tokens(
    request: Request,
    refresh_token: str | None = Cookie(default=None),
):
    _rate_limit(_get_client_ip(request))

    # Cookie (web) primero; cuerpo (nativo, SEC-06) como alternativa.
    refresh_token = refresh_token or await _refresh_token_from_body(request)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    try:
        payload = jwt.decode(
            refresh_token,
            JWT_SECRET,
            algorithms=["HS256"],
            audience=TOKEN_AUDIENCE,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")

    jti = payload.get("jti")
    steam_id: str = payload["sub"]
    if not jti:
        raise HTTPException(status_code=401, detail="Refresh token revoked or reused")

    # Rotación: consume borra el JTI en la misma sentencia que lo valida (un solo uso).
    with session_store():
        vigente = await refresh_repo.consume(jti, steam_id)
    if not vigente:
        # Nunca emitido, ya rotado o revocado
        raise HTTPException(status_code=401, detail="Refresh token revoked or reused")

    access_token, new_refresh_token = await _issue_tokens(steam_id)

    return _token_response(request, access_token, new_refresh_token)


@router.post("/auth/logout", summary="Revoca el refresh token y limpia la cookie")
async def logout(
    request: Request,
    refresh_token: str | None = Cookie(default=None),
):
    await _revoke_refresh(refresh_token or await _refresh_token_from_body(request))

    response = JSONResponse({"message": "Logged out"})
    _delete_refresh_cookie(response)
    return response


def _delete_refresh_cookie(response: JSONResponse) -> None:
    # Mismos atributos con los que se emitió: si no coinciden, el navegador no
    # la considera la misma cookie y el borrado no surte efecto.
    response.delete_cookie(
        key="refresh_token",
        path="/",
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
    )


async def _revoke_refresh(refresh_token: str | None) -> None:
    if not refresh_token:
        return
    try:
        payload = jwt.decode(refresh_token, JWT_SECRET, algorithms=["HS256"], audience=TOKEN_AUDIENCE)
    except jwt.InvalidTokenError:
        return  # inválido o caducado: no hay JTI que revocar
    jti = payload.get("jti")
    if jti:
        with session_store():
            await refresh_repo.revoke(jti)


@router.delete("/me", summary="Borra los datos del usuario y cierra la sesión (LAUNCH-04)")
async def delete_me(
    request: Request,
    refresh_token: str | None = Cookie(default=None),
    claims: dict = Depends(require_jwt),
):
    """Borrado de cuenta que exige Google Play y describe la política de privacidad.

    Todo lo que lleva SteamID en servidor: tokens de dispositivo (push), alertas
    de precio y el histórico de la cartera (UX-16), más las cachés en memoria de
    perfil e inventario y el refresh vigente. Los precios históricos de mercado no
    llevan SteamID y se quedan.
    Idempotente: borrar lo que ya no existe también responde 200.
    """
    steam_id: str = claims["sub"]
    await notifications_repo.delete_device_tokens_for(steam_id)
    await alerts_repo.delete_all_for_user(steam_id)
    await portfolio_repo.delete_all_for_user(steam_id)
    await inventory_snapshot_repo.delete_for_user(steam_id)
    with session_store():
        await refresh_repo.delete_all_for_user(steam_id)
    for cache in (_profile_cache, _inventory_cache, _inventory_refresh_cooldown):
        cache.pop(steam_id, None)
    await _revoke_refresh(refresh_token or await _refresh_token_from_body(request))

    response = JSONResponse({"message": "Deleted"})
    _delete_refresh_cookie(response)
    return response
