"""Invariantes de los endpoints de `auth/router.py`.

Ojo con cómo se parchea la configuración aquí: `auth/router.py` hace
`from settings import REVIEW_USER, ...`, que copia el **valor** en el namespace
del módulo al importarlo. Parchear `settings.REVIEW_USER` (o un `setenv`) no
tiene ningún efecto sobre el router; hay que parchear `auth.router.REVIEW_USER`.
`DEBUG` es la excepción: el router lo lee con `os.getenv()` en cada request, así
que ahí sí vale `monkeypatch.setenv`.
"""
import time

import jwt
import pytest

import auth.router as auth_router
from settings import JWT_SECRET
from stores import CODE_TTL, TOKEN_AUDIENCE, _auth_codes, _refresh_store

STEAM_ID = "76561198000000000"


def _decode(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=TOKEN_AUDIENCE)


def _seed_code(code: str = "un-codigo", steam_id: str = STEAM_ID, ttl: float = CODE_TTL) -> str:
    """Coloca un auth code válido como lo habría dejado el callback de Steam."""
    _auth_codes[code] = (steam_id, time.monotonic() + ttl)
    return code


# ── /auth/token: el auth code es de un solo uso ───────────────────────────────

def test_token_exchange_returns_access_token_and_refresh_cookie(client):
    resp = client.post("/auth/token", json={"code": _seed_code()})

    assert resp.status_code == 200
    payload = _decode(resp.json()["access_token"])
    assert payload["sub"] == STEAM_ID
    assert payload["type"] == "access"

    cookie = resp.cookies.get("refresh_token")
    assert cookie
    assert _decode(cookie)["type"] == "refresh"


def test_auth_code_cannot_be_exchanged_twice(client):
    code = _seed_code()

    assert client.post("/auth/token", json={"code": code}).status_code == 200

    second = client.post("/auth/token", json={"code": code})
    assert second.status_code == 400
    assert second.json()["detail"] == "Invalid or already used code"


def test_expired_auth_code_is_rejected(client):
    """TTL de 30 s: un code caducado no se canjea aunque exista en el store."""
    code = _seed_code(ttl=-1)

    resp = client.post("/auth/token", json={"code": code})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Code expired"


def test_unknown_auth_code_is_rejected(client):
    resp = client.post("/auth/token", json={"code": "jamas-emitido"})
    assert resp.status_code == 400


def test_missing_auth_code_is_rejected(client):
    resp = client.post("/auth/token", json={})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Missing code"


# ── /auth/refresh: rotación de JTI ────────────────────────────────────────────

def _login(client) -> str:
    """Hace un canje completo y devuelve el refresh token resultante."""
    resp = client.post("/auth/token", json={"code": _seed_code(f"code-{time.monotonic()}")})
    assert resp.status_code == 200
    return resp.cookies["refresh_token"]


def test_refresh_rotates_the_jti(client):
    old_refresh = _login(client)
    old_jti = _decode(old_refresh)["jti"]

    resp = client.post("/auth/refresh", cookies={"refresh_token": old_refresh})

    assert resp.status_code == 200
    new_jti = _decode(resp.cookies["refresh_token"])["jti"]
    assert new_jti != old_jti
    assert old_jti not in _refresh_store
    assert new_jti in _refresh_store


def test_old_refresh_token_stops_working_after_rotation(client):
    """El invariante de verdad: reusar el refresh anterior tiene que fallar."""
    old_refresh = _login(client)
    assert client.post("/auth/refresh", cookies={"refresh_token": old_refresh}).status_code == 200

    replay = client.post("/auth/refresh", cookies={"refresh_token": old_refresh})

    assert replay.status_code == 401
    assert replay.json()["detail"] == "Refresh token revoked or reused"


def test_refresh_without_cookie_is_rejected(client):
    resp = client.post("/auth/refresh")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Missing refresh token"


def test_access_token_is_not_accepted_as_a_refresh_token(client):
    resp = client.post("/auth/token", json={"code": _seed_code()})
    access = resp.json()["access_token"]

    replayed = client.post("/auth/refresh", cookies={"refresh_token": access})

    assert replayed.status_code == 401
    assert replayed.json()["detail"] == "Invalid token type"


def test_refresh_token_signed_with_another_secret_is_rejected(client):
    forged = jwt.encode(
        {"sub": STEAM_ID, "type": "refresh", "aud": TOKEN_AUDIENCE, "jti": "inventado",
         "exp": time.time() + 3600},
        "secreto-del-atacante", algorithm="HS256",
    )

    resp = client.post("/auth/refresh", cookies={"refresh_token": forged})

    assert resp.status_code == 401


def test_refresh_with_valid_signature_but_unknown_jti_is_rejected(client):
    """Token bien firmado pero cuyo jti nunca se registró (o ya se revocó)."""
    valid = jwt.encode(
        {"sub": STEAM_ID, "type": "refresh", "aud": TOKEN_AUDIENCE, "jti": "nunca-registrado",
         "exp": time.time() + 3600},
        JWT_SECRET, algorithm="HS256",
    )

    resp = client.post("/auth/refresh", cookies={"refresh_token": valid})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Refresh token revoked or reused"


# ── /auth/logout: revocación ──────────────────────────────────────────────────

def test_logout_revokes_the_refresh_token(client):
    refresh = _login(client)
    jti = _decode(refresh)["jti"]

    resp = client.post("/auth/logout", cookies={"refresh_token": refresh})

    assert resp.status_code == 200
    assert jti not in _refresh_store


def test_refresh_after_logout_fails(client):
    refresh = _login(client)
    assert client.post("/auth/logout", cookies={"refresh_token": refresh}).status_code == 200

    resp = client.post("/auth/refresh", cookies={"refresh_token": refresh})

    assert resp.status_code == 401


def test_logout_without_cookie_still_succeeds(client):
    assert client.post("/auth/logout").status_code == 200


# ── /auth/dev-token: 404 fuera de DEBUG ───────────────────────────────────────

def test_dev_token_is_404_when_debug_is_false(client, monkeypatch):
    monkeypatch.setenv("DEBUG", "false")

    resp = client.post("/auth/dev-token", json={"steam_id": STEAM_ID})

    assert resp.status_code == 404


def test_dev_token_is_404_when_debug_is_unset(client, monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)

    resp = client.post("/auth/dev-token", json={"steam_id": STEAM_ID})

    assert resp.status_code == 404


def test_dev_token_issues_tokens_when_debug_is_true(client, monkeypatch):
    monkeypatch.setenv("DEBUG", "true")

    resp = client.post("/auth/dev-token", json={"steam_id": STEAM_ID})

    assert resp.status_code == 200
    assert _decode(resp.json()["access_token"])["sub"] == STEAM_ID


def test_dev_token_rejects_a_malformed_steam_id(client, monkeypatch):
    monkeypatch.setenv("DEBUG", "true")

    resp = client.post("/auth/dev-token", json={"steam_id": "123"})

    assert resp.status_code == 400


# ── /auth/review-login: 404 sin configurar, 401 con credenciales malas ────────

@pytest.fixture
def review_configured(monkeypatch):
    """Configura las tres vars de review en el namespace del router."""
    monkeypatch.setattr(auth_router, "REVIEW_USER", "revisor")
    monkeypatch.setattr(auth_router, "REVIEW_PASSWORD", "clave-secreta")
    monkeypatch.setattr(auth_router, "REVIEW_STEAM_ID", STEAM_ID)


@pytest.mark.parametrize("missing", ["REVIEW_USER", "REVIEW_PASSWORD", "REVIEW_STEAM_ID"])
def test_review_login_is_404_when_any_var_is_missing(client, review_configured, monkeypatch, missing):
    """Basta con que falte una de las tres para que el endpoint no exista."""
    monkeypatch.setattr(auth_router, missing, "")

    resp = client.post("/auth/review-login", json={"user": "revisor", "password": "clave-secreta"})

    assert resp.status_code == 404


def test_review_login_succeeds_with_the_right_credentials(client, review_configured):
    resp = client.post("/auth/review-login", json={"user": "revisor", "password": "clave-secreta"})

    assert resp.status_code == 200
    assert _decode(resp.json()["access_token"])["sub"] == STEAM_ID


def test_review_login_rejects_a_wrong_password(client, review_configured):
    resp = client.post("/auth/review-login", json={"user": "revisor", "password": "equivocada"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid review credentials"


def test_review_login_rejects_a_wrong_user(client, review_configured):
    resp = client.post("/auth/review-login", json={"user": "otro", "password": "clave-secreta"})

    assert resp.status_code == 401


def test_review_login_rejects_empty_credentials(client, review_configured):
    resp = client.post("/auth/review-login", json={})

    assert resp.status_code == 401


def test_review_login_rejects_a_password_prefix(client, review_configured):
    """Un prefijo de la contraseña correcta no cuela.

    No distingue `compare_digest` de `==` — ambos rechazan un prefijo. Cubre
    que la comparación sea sobre el valor completo, nada más.
    """
    resp = client.post("/auth/review-login", json={"user": "revisor", "password": "clave-secret"})

    assert resp.status_code == 401


def test_review_login_uses_a_constant_time_comparison(client, review_configured):
    """Prueba de mutación de CAL-01: detecta `compare_digest` → `==`.

    `secrets.compare_digest` sobre `str` sólo admite ASCII; con un carácter
    fuera de ese rango lanza `TypeError`. La app no tiene handler para él, así
    que TestClient lo propaga en vez de devolver una respuesta.

    Ese `TypeError` es la huella observable de la comparación en tiempo
    constante: `==` compara cualquier str sin quejarse y devolvería un 401
    normal. Por eso el test afirma la excepción y no un código de estado — si
    alguien sustituye `compare_digest` por `==`, aquí deja de haber excepción y
    el test falla.

    Ojo: esto documenta el comportamiento actual, no lo bendice. Un anónimo
    puede provocar el TypeError pre-auth mandando `user` con un acento. Es un
    500 (o un crash del worker) alcanzable sin credenciales — ver nota en
    docs/handoff.md.
    """
    with pytest.raises(TypeError, match="non-ASCII"):
        client.post("/auth/review-login", json={"user": "revisör", "password": "clave-secreta"})


def test_review_login_rejects_non_string_credentials(client, review_configured):
    """Un JSON con tipos raros no puede tumbar el endpoint con un 500."""
    resp = client.post("/auth/review-login", json={"user": 12345, "password": ["lista"]})

    assert resp.status_code == 401
