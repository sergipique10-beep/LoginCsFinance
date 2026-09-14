"""Invariantes de emisión y validación de tokens (`auth/service.py`).

Estos tests no miran líneas cubiertas: miran las propiedades que, si se rompen,
no producen ningún error visible — solo dejan de proteger.
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from auth.service import _consume_nonce, _issue_nonce, _issue_tokens, require_jwt
from settings import JWT_SECRET
from stores import (
    ACCESS_TOKEN_TTL, NONCE_TTL, REFRESH_TOKEN_TTL, TOKEN_AUDIENCE,
    _nonces, _refresh_store,
)

STEAM_ID = "76561198000000000"


def _decode(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=TOKEN_AUDIENCE)


def _bearer(token: str):
    """Envuelve un token como lo entregaría HTTPBearer a require_jwt."""
    from fastapi.security import HTTPAuthorizationCredentials
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


# ── Claims y caducidades ──────────────────────────────────────────────────────

def test_access_token_carries_expected_claims():
    access, _ = _issue_tokens(STEAM_ID)
    payload = _decode(access)

    assert payload["sub"] == STEAM_ID
    assert payload["type"] == "access"
    assert payload["aud"] == TOKEN_AUDIENCE
    # No existe un claim steam_id aparte: el SteamID vive solo en `sub`.
    assert "steam_id" not in payload


def test_access_token_expires_in_thirty_minutes():
    access, _ = _issue_tokens(STEAM_ID)
    payload = _decode(access)

    ttl = payload["exp"] - payload["iat"]
    assert ttl == int(ACCESS_TOKEN_TTL.total_seconds()) == 1800


def test_refresh_token_carries_jti_and_expires_in_seven_days():
    _, refresh = _issue_tokens(STEAM_ID)
    payload = _decode(refresh)

    assert payload["type"] == "refresh"
    assert payload["jti"]
    ttl = payload["exp"] - payload["iat"]
    assert ttl == int(REFRESH_TOKEN_TTL.total_seconds()) == 7 * 24 * 3600


def test_each_issue_produces_a_unique_jti():
    _, first = _issue_tokens(STEAM_ID)
    _, second = _issue_tokens(STEAM_ID)

    assert _decode(first)["jti"] != _decode(second)["jti"]


def test_issued_jti_is_registered_for_revocation():
    """Si el jti no entra en el store, /auth/refresh lo rechazaría siempre."""
    _, refresh = _issue_tokens(STEAM_ID)
    assert _decode(refresh)["jti"] in _refresh_store


# ── require_jwt: lo que tiene que rechazar ────────────────────────────────────

def test_require_jwt_accepts_a_fresh_access_token():
    access, _ = _issue_tokens(STEAM_ID)
    payload = require_jwt(_bearer(access))
    assert payload["sub"] == STEAM_ID


def test_require_jwt_rejects_a_refresh_token_presented_as_access():
    """Una cookie robada no puede servir para llamar a la API."""
    _, refresh = _issue_tokens(STEAM_ID)

    with pytest.raises(HTTPException) as exc:
        require_jwt(_bearer(refresh))

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid token type"


def test_require_jwt_rejects_a_token_with_a_different_audience():
    now = datetime.now(timezone.utc)
    foreign = jwt.encode(
        {"sub": STEAM_ID, "type": "access", "aud": "otra-app",
         "iat": now, "exp": now + timedelta(minutes=30)},
        JWT_SECRET, algorithm="HS256",
    )

    with pytest.raises(HTTPException) as exc:
        require_jwt(_bearer(foreign))

    assert exc.value.status_code == 401


def test_require_jwt_rejects_an_expired_token():
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    expired = jwt.encode(
        {"sub": STEAM_ID, "type": "access", "aud": TOKEN_AUDIENCE,
         "iat": past, "exp": past + timedelta(minutes=30)},
        JWT_SECRET, algorithm="HS256",
    )

    with pytest.raises(HTTPException) as exc:
        require_jwt(_bearer(expired))

    assert exc.value.status_code == 401
    assert exc.value.detail == "Token expired"


def test_require_jwt_rejects_a_token_signed_with_another_secret():
    """El invariante que protege contra un atacante que fabrica sus propios tokens."""
    now = datetime.now(timezone.utc)
    forged = jwt.encode(
        {"sub": STEAM_ID, "type": "access", "aud": TOKEN_AUDIENCE,
         "iat": now, "exp": now + timedelta(minutes=30)},
        "el-secreto-del-atacante-no-el-nuestro", algorithm="HS256",
    )

    with pytest.raises(HTTPException) as exc:
        require_jwt(_bearer(forged))

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid token"


def test_require_jwt_rejects_an_unsigned_none_algorithm_token():
    """`alg: none` es el ataque clásico contra JWT: firma vacía, payload libre."""
    unsigned = jwt.encode(
        {"sub": STEAM_ID, "type": "access", "aud": TOKEN_AUDIENCE},
        key="", algorithm="none",
    )

    with pytest.raises(HTTPException) as exc:
        require_jwt(_bearer(unsigned))

    assert exc.value.status_code == 401


# ── Nonces ────────────────────────────────────────────────────────────────────

def test_nonce_roundtrip_returns_the_sealed_redirect_origin():
    nonce = _issue_nonce("https://app.example.com")
    assert _consume_nonce(nonce) == "https://app.example.com"


def test_nonce_cannot_be_reused():
    """Un nonce consumido dos veces sería un CSRF replay."""
    nonce = _issue_nonce("https://app.example.com")

    assert _consume_nonce(nonce) == "https://app.example.com"
    assert _consume_nonce(nonce) is None


def test_unknown_nonce_is_rejected():
    assert _consume_nonce("jamas-emitido") is None


def test_expired_nonce_is_rejected(monkeypatch):
    import auth.service as service

    nonce = _issue_nonce("https://app.example.com")
    issued_at, origin = _nonces[nonce]
    # Reescribe el instante de emisión para colocarlo justo fuera del TTL.
    _nonces[nonce] = (issued_at - NONCE_TTL - 1, origin)

    assert _consume_nonce(nonce) is None


def test_each_nonce_is_unique():
    assert _issue_nonce("https://a.example") != _issue_nonce("https://a.example")
