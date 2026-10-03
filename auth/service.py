import secrets
import time
import logging
from contextlib import contextmanager
from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from settings import COOKIE_SECURE, JWT_SECRET
from stores import (
    NONCE_TTL, CODE_TTL,
    RATE_LIMIT_CALLS, RATE_LIMIT_WINDOW, MARKET_RATE_LIMIT_CALLS, STATS_RATE_LIMIT_CALLS,
    ACCESS_TOKEN_TTL, REFRESH_TOKEN_TTL, TOKEN_AUDIENCE,
    _nonces, _rate_store,
)
from auth import refresh_repo

logger = logging.getLogger("uvicorn.error")


@contextmanager
def session_store():
    """Traduce un fallo de Supabase en el store de refresh a 503, nunca a 401 (SEC-11).

    Un 401 hace que el cliente nativo borre el refresh guardado y la sesión se pierde
    de verdad; con un 503 lo conserva y reintenta en la siguiente apertura.
    Envolver SOLO la llamada al repo: un HTTPException de dentro también se tragaría.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — red, credenciales o PostgREST: todo es "no disponible"
        logger.error("refresh store no disponible: %r", exc)
        raise HTTPException(status_code=503, detail="Session store unavailable") from exc


def token_matches(given: str | None, expected: str) -> bool:
    """Comparación en tiempo constante del token de un endpoint interno.

    Sobre bytes, no str: `compare_digest` con un `str` no-ASCII lanza TypeError,
    y Starlette decodifica cabeceras como latin-1, así que un byte alto crudo en
    la cabecera era un 500 alcanzable sin credenciales (SEC-04/SEC-05). Con el
    token esperado vacío el endpoint está deshabilitado: siempre False.
    """
    if not expected or not given:
        return False
    return secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _get_client_ip(request: Request) -> str:
    """Returns the real client IP honoring trusted reverse-proxy headers.

    Only the first value of X-Forwarded-For is used to prevent spoofing.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit(ip: str, *, limit: int = RATE_LIMIT_CALLS, bucket: str = "") -> None:
    """Ventana deslizante por IP. `bucket` separa presupuestos: el de auth (10/min,
    operaciones sensibles) no debe compartir contador con el de mercado (SEC-03)."""
    key = f"{bucket}:{ip}" if bucket else ip
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW
    calls = [t for t in _rate_store[key] if t > cutoff]
    if len(calls) >= limit:
        raise HTTPException(status_code=429, detail="Too many requests")
    calls.append(now)
    _rate_store[key] = calls


def market_rate_limit(request: Request) -> None:
    """SEC-03 — dependencia para las lecturas de /market/*.

    Todos exigen Bearer, pero un cliente en bucle (o un bug de reintentos del
    frontend) agota el presupuesto compartido de steamwebapi para todos. 60/60 s
    por IP: la pantalla de Market abre ~6 llamadas distintas, un usuario normal
    no se acerca. Se declara como `dependencies=[Depends(market_rate_limit)]`
    en el decorador para no tocar la firma de cada handler.
    """
    _rate_limit(_get_client_ip(request), limit=MARKET_RATE_LIMIT_CALLS, bucket="market")


def leetify_rate_limit(request: Request) -> None:
    """SEC-09 — dependencia del router de /me/stats*. Cada llamada que no acierta
    la caché consume cuota de Leetify, así que tiene su propio cupo por IP."""
    _rate_limit(_get_client_ip(request), limit=STATS_RATE_LIMIT_CALLS, bucket="stats")


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


async def _issue_tokens(steam_id: str) -> tuple[str, str]:
    """Issues an (access_token, refresh_token) pair for the given steam_id.

    The access_token carries type="access" and expires in ACCESS_TOKEN_TTL.
    The refresh_token carries type="refresh", a unique jti, and expires in REFRESH_TOKEN_TTL.
    The jti is persisted in Supabase (SEC-11) to allow rotation and revocation.
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

    with session_store():
        await refresh_repo.save(jti, steam_id, refresh_exp)

    return access_token, refresh_token


def _set_refresh_cookie(response: JSONResponse, refresh_token: str) -> None:
    """Attaches the HttpOnly cookie that carries the refresh token."""
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        max_age=int(REFRESH_TOKEN_TTL.total_seconds()),
        path="/",  # "/" because the Angular proxy rewrites /api/auth/* → /auth/*
    )


# ── SEC-06: el cliente nativo no puede usar la cookie ─────────────────────────
#
# El WebView de Capacitor pide desde https://localhost, cross-site respecto a la
# API, y Chromium rechaza un Set-Cookie SameSite=Strict en esa situación (medido
# en dispositivo: `SchemefulSameSiteStrict`). Para ese origen el refresh viaja en
# el cuerpo JSON, y el cliente lo guarda en almacenamiento privado de la app.
# `Origin` lo pone el navegador y una web ajena no puede falsificarlo; un cliente
# no-navegador que lo ponga a mano no gana nada que no tuviera ya con la cookie.
NATIVE_ORIGIN = "https://localhost"


def _is_native_client(request: Request) -> bool:
    return request.headers.get("origin", "").lower() == NATIVE_ORIGIN


def _token_response(request: Request, access_token: str, refresh_token: str) -> JSONResponse:
    """Entrega el par de tokens: refresh en el cuerpo para nativo, en cookie HttpOnly para web."""
    if _is_native_client(request):
        return JSONResponse({"access_token": access_token, "refresh_token": refresh_token})
    response = JSONResponse({"access_token": access_token})
    _set_refresh_cookie(response, refresh_token)
    return response


async def _refresh_token_from_body(request: Request) -> str | None:
    """Refresh token del cuerpo JSON (`{"refresh_token": "..."}`), o None si no hay/está mal formado."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — cuerpo vacío o no-JSON: simplemente no hay token
        return None
    token = body.get("refresh_token") if isinstance(body, dict) else None
    return token if isinstance(token, str) and token else None


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
