import os
import re
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

import jwt
from settings import (
    ALLOWED_REDIRECT_ORIGINS,
    BASE_URL,
    FRONTEND_URL,
    JWT_SECRET,
    REVIEW_PASSWORD,
    REVIEW_STEAM_ID,
    REVIEW_USER,
)
from stores import _auth_codes, CODE_TTL, _refresh_store, TOKEN_AUDIENCE
from auth.service import (
    _consume_nonce,
    _get_client_ip,
    _issue_nonce,
    _issue_tokens,
    _rate_limit,
    _set_refresh_cookie,
)

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


@router.post("/auth/token", summary="Canjea el auth code por access token + refresh cookie")
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

    access_token, refresh_token = _issue_tokens(steam_id)

    response = JSONResponse({"access_token": access_token})
    _set_refresh_cookie(response, refresh_token)
    return response


@router.post("/auth/dev-token", summary="[DEV ONLY] Emite tokens para un steam_id sin pasar por Steam OpenID")
async def dev_token(request: Request):
    # Only active when DEBUG=true in .env — returns 404 in any other environment
    if os.getenv("DEBUG", "false").lower() != "true":
        raise HTTPException(status_code=404, detail="Not found")

    body = await request.json()
    steam_id: str = body.get("steam_id", "")

    if not re.match(r"^\d{17}$", steam_id):
        raise HTTPException(status_code=400, detail="steam_id must be exactly 17 digits")

    access_token, refresh_token = _issue_tokens(steam_id)
    response = JSONResponse({"access_token": access_token})
    _set_refresh_cookie(response, refresh_token)
    return response


@router.post("/auth/review-login", summary="Acceso de revisión (Google Play) sin Steam")
async def review_login(request: Request):
    _rate_limit(_get_client_ip(request))
    if not (REVIEW_USER and REVIEW_PASSWORD and REVIEW_STEAM_ID):
        raise HTTPException(status_code=404, detail="Not found")

    body = await request.json()
    user = body.get("user", "")
    password = body.get("password", "")
    if not (secrets.compare_digest(user, REVIEW_USER)
            and secrets.compare_digest(password, REVIEW_PASSWORD)):
        raise HTTPException(status_code=401, detail="Invalid review credentials")

    access_token, refresh_token = _issue_tokens(REVIEW_STEAM_ID)
    response = JSONResponse({"access_token": access_token})
    _set_refresh_cookie(response, refresh_token)
    return response


@router.post("/auth/refresh", summary="Rota el refresh token y devuelve nuevo access token")
async def refresh_tokens(
    request: Request,
    refresh_token: str | None = Cookie(default=None),
):
    _rate_limit(_get_client_ip(request))

    if not refresh_token:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    # Lazy cleanup: purge expired JTIs before operating on the store
    now_mono = time.monotonic()
    expired_jtis = [jti for jti, exp in _refresh_store.items() if now_mono > exp]
    for jti in expired_jtis:
        del _refresh_store[jti]

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
    if not jti or jti not in _refresh_store:
        # JTI not found: never valid, already rotated, or revoked
        raise HTTPException(status_code=401, detail="Refresh token revoked or reused")

    steam_id: str = payload["sub"]

    # Revoke previous JTI (rotation: each refresh_token is single-use)
    del _refresh_store[jti]

    access_token, new_refresh_token = _issue_tokens(steam_id)

    response = JSONResponse({"access_token": access_token})
    _set_refresh_cookie(response, new_refresh_token)
    return response


@router.post("/auth/logout", summary="Revoca el refresh token y limpia la cookie")
async def logout(
    refresh_token: str | None = Cookie(default=None),
):
    if refresh_token:
        try:
            payload = jwt.decode(
                refresh_token,
                JWT_SECRET,
                algorithms=["HS256"],
                audience=TOKEN_AUDIENCE,
            )
            jti = payload.get("jti")
            if jti:
                _refresh_store.pop(jti, None)
        except jwt.InvalidTokenError:
            # Invalid or expired token: no JTI to revoke, continue anyway
            pass

    response = JSONResponse({"message": "Logged out"})
    response.delete_cookie(
        key="refresh_token",
        path="/",
        httponly=True,
        secure=False,  # TODO prod: change to True (see CLAUDE.md § Pendiente para producción)
        samesite="strict",
    )
    return response
