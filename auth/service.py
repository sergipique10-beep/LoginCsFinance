import secrets
import time
import logging
from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from settings import JWT_SECRET
from stores import (
    NONCE_TTL, CODE_TTL,
    RATE_LIMIT_CALLS, RATE_LIMIT_WINDOW,
    ACCESS_TOKEN_TTL, REFRESH_TOKEN_TTL, TOKEN_AUDIENCE,
    _nonces, _refresh_store, _rate_store,
)

logger = logging.getLogger("uvicorn.error")


def _get_client_ip(request: Request) -> str:
    """Returns the real client IP honoring trusted reverse-proxy headers.

    Only the first value of X-Forwarded-For is used to prevent spoofing.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit(ip: str) -> None:
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW
    calls = [t for t in _rate_store[ip] if t > cutoff]
    if len(calls) >= RATE_LIMIT_CALLS:
        raise HTTPException(status_code=429, detail="Too many requests")
    calls.append(now)
    _rate_store[ip] = calls


def _issue_nonce(redirect_origin: str) -> str:
    nonce = secrets.token_urlsafe(32)
    _nonces[nonce] = (time.monotonic(), redirect_origin)
    return nonce


def _consume_nonce(nonce: str) -> str | None:
    """Consumes the nonce and returns the associated redirect_origin, or None if invalid/expired."""
    now = time.monotonic()
    for k in [k for k, (t, _) in _nonces.items() if now - t > NONCE_TTL]:
        del _nonces[k]
    entry = _nonces.pop(nonce, None)
    if entry is None:
        return None
    issued_at, redirect_origin = entry
    if now - issued_at > NONCE_TTL:
        return None
    return redirect_origin


def _issue_tokens(steam_id: str) -> tuple[str, str]:
    """Issues an (access_token, refresh_token) pair for the given steam_id.

    The access_token carries type="access" and expires in ACCESS_TOKEN_TTL.
    The refresh_token carries type="refresh", a unique jti, and expires in REFRESH_TOKEN_TTL.
    The jti is registered in _refresh_store to allow revocation.
    """
    now = datetime.now(timezone.utc)

    access_token = jwt.encode(
        {
            "sub": steam_id,
            "type": "access",
            "aud": TOKEN_AUDIENCE,
            "iat": now,
            "exp": now + ACCESS_TOKEN_TTL,
        },
        JWT_SECRET,
        algorithm="HS256",
    )

    jti = secrets.token_urlsafe(32)
    refresh_exp = now + REFRESH_TOKEN_TTL

    refresh_token = jwt.encode(
        {
            "sub": steam_id,
            "type": "refresh",
            "aud": TOKEN_AUDIENCE,
            "jti": jti,
            "iat": now,
            "exp": refresh_exp,
        },
        JWT_SECRET,
        algorithm="HS256",
    )

    _refresh_store[jti] = time.monotonic() + REFRESH_TOKEN_TTL.total_seconds()

    return access_token, refresh_token


def _set_refresh_cookie(response: JSONResponse, refresh_token: str) -> None:
    """Attaches the HttpOnly cookie that carries the refresh token."""
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=False,  # TODO prod: change to True (see CLAUDE.md § Pendiente para producción)
        samesite="strict",
        max_age=int(REFRESH_TOKEN_TTL.total_seconds()),
        path="/",  # "/" because the Angular proxy rewrites /api/auth/* → /auth/*
    )


# ── JWT dependency (protected routes) ─────────────────────────────────────────

_bearer = HTTPBearer()


def require_jwt(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """Validates the Bearer token and ensures it is of type 'access'.

    Explicitly rejects refresh tokens presented as access tokens, preventing
    a stolen cookie token from being used for API calls.
    """
    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET,
            algorithms=["HS256"],
            audience=TOKEN_AUDIENCE,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")

    return payload
