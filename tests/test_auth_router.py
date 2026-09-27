"""Invariantes de los endpoints de `auth/router.py`.

Ojo con cómo se parchea la configuración aquí: `auth/router.py` hace
`from settings import REVIEW_USER, ...`, que copia el **valor** en el namespace
del módulo al importarlo. Parchear `settings.REVIEW_USER` (o un `setenv`) no
tiene ningún efecto sobre el router; hay que parchear `auth.router.REVIEW_USER`.

Eso vale también para el dev-token: desde SEC-02 el router lee
`DEV_TOKEN_ENABLED` (constante calculada en `settings.py` a partir de `DEBUG` y
`ENV`), no `os.getenv("DEBUG")` en cada request. Un `monkeypatch.setenv` ya no
tiene efecto — hay que parchear `auth.router.DEV_TOKEN_ENABLED`. La combinatoria
DEBUG × ENV se prueba aparte, sobre `settings.py` directamente.
"""
import secrets
import time

import jwt
import pytest

import auth.router as auth_router
import auth.service as auth_service
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


# ── DELETE /me: borrado de cuenta (LAUNCH-04) ────────────────────────────────

def test_delete_me_wipes_tokens_alerts_caches_and_revokes_refresh(client, monkeypatch):
    """Todo lo que lleva SteamID en servidor se va en una sola llamada: es lo que
    la política de privacidad promete y lo que Google Play exige poder hacer."""
    from unittest.mock import AsyncMock
    from stores import _profile_cache, _inventory_cache, _inventory_refresh_cooldown
    from tests.conftest import STEAM_ID as JWT_SUB  # el sub que firma el fixture `client`

    tokens = AsyncMock()
    alerts = AsyncMock()
    monkeypatch.setattr(auth_router.notifications_repo, "delete_device_tokens_for", tokens)
    monkeypatch.setattr(auth_router.alerts_repo, "delete_all_for_user", alerts)
    _profile_cache[JWT_SUB] = ({"name": "x"}, 0.0)
    _inventory_cache[JWT_SUB] = ([], 0.0)
    _inventory_refresh_cooldown[JWT_SUB] = 0.0
    refresh = _login(client)
    jti = _decode(refresh)["jti"]

    resp = client.delete("/me", cookies={"refresh_token": refresh})

    assert resp.status_code == 200
    tokens.assert_awaited_once_with(JWT_SUB)
    alerts.assert_awaited_once_with(JWT_SUB)
    assert JWT_SUB not in _profile_cache and JWT_SUB not in _inventory_cache
    assert JWT_SUB not in _inventory_refresh_cooldown
    assert jti not in _refresh_store
    assert "refresh_token=" in resp.headers["set-cookie"]  # cookie borrada


def test_delete_me_requires_a_session(monkeypatch):
    from unittest.mock import AsyncMock
    from fastapi.testclient import TestClient
    import main as main_module

    monkeypatch.setattr(main_module, "_fetch_static_images", AsyncMock())
    tokens = AsyncMock()
    monkeypatch.setattr(auth_router.notifications_repo, "delete_device_tokens_for", tokens)
    with TestClient(main_module.app) as anon:
        assert anon.delete("/me").status_code == 401
    tokens.assert_not_awaited()


# ── /auth/dev-token: 404 fuera de DEBUG ───────────────────────────────────────

def test_dev_token_is_404_when_disabled(client, monkeypatch):
    monkeypatch.setattr(auth_router, "DEV_TOKEN_ENABLED", False)

    resp = client.post("/auth/dev-token", json={"steam_id": STEAM_ID})

    assert resp.status_code == 404


def test_dev_token_issues_tokens_when_enabled(client, monkeypatch):
    monkeypatch.setattr(auth_router, "DEV_TOKEN_ENABLED", True)

    resp = client.post("/auth/dev-token", json={"steam_id": STEAM_ID})

    assert resp.status_code == 200
    assert _decode(resp.json()["access_token"])["sub"] == STEAM_ID


def test_dev_token_rejects_a_malformed_steam_id(client, monkeypatch):
    monkeypatch.setattr(auth_router, "DEV_TOKEN_ENABLED", True)

    resp = client.post("/auth/dev-token", json={"steam_id": "123"})

    assert resp.status_code == 400


@pytest.mark.parametrize(
    "env, debug, enabled",
    [
        ("production", "true", False),   # SEC-02: el guardarraíl que importa
        ("production", "false", False),
        ("development", "true", True),
        ("development", "false", False),
        (None, "true", True),            # ENV sin definir → development
        ("production", None, False),
    ],
)
def test_dev_token_enabled_requires_debug_and_non_production(monkeypatch, env, debug, enabled):
    """La combinatoria DEBUG x ENV, sobre `settings.py` (donde se calcula).

    El caso que da sentido a SEC-02 es el primero: un DEBUG=true colado en
    Render no basta para revivir el endpoint si ENV=production.
    """
    import importlib

    import settings

    for name, value in (("ENV", env), ("DEBUG", debug)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    # load_dotenv() no pisa lo que ya está en el entorno, así que el setenv manda.
    reloaded = importlib.reload(settings)

    try:
        assert reloaded.DEV_TOKEN_ENABLED is enabled
    finally:
        importlib.reload(settings)


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


def test_review_login_uses_a_constant_time_comparison(client, review_configured, monkeypatch):
    """Prueba de mutación: detecta `compare_digest` → `==`.

    Antes de SEC-04 esto se afirmaba vía el `TypeError` que lanza
    `compare_digest` sobre un `str` no-ASCII. Esa huella ya no existe: ahora se
    compara sobre `bytes`, que aceptan cualquier carácter (y ese era justo el
    bug — un anónimo tumbaba el endpoint con un acento).

    La vía de recambio es un espía: se envuelve `secrets.compare_digest` y se
    exige que el endpoint pase por él. Sustituirlo por `==` deja el contador a
    cero y el test falla, que es lo que tiene que seguir cazando.
    """
    llamadas = []
    real = secrets.compare_digest

    def espia(a, b):
        llamadas.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth_router.secrets, "compare_digest", espia)

    resp = client.post("/auth/review-login", json={"user": "revisor", "password": "clave-secreta"})

    assert resp.status_code == 200
    assert llamadas, "review-login no pasó por secrets.compare_digest"
    # Sobre bytes: es lo que quita la restricción ASCII sin perder el tiempo constante.
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in llamadas)


def test_review_login_rejects_non_ascii_credentials_with_401(client, review_configured):
    """SEC-04: el bug original — un acento provocaba un TypeError → 500 pre-auth.

    Es una credencial equivocada, no un error del servidor: 401.
    """
    resp = client.post("/auth/review-login", json={"user": "revisör", "password": "clave-secreta"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid review credentials"


def test_review_login_accepts_non_ascii_correct_credentials(client, monkeypatch):
    """El reverso: si las credenciales configuradas llevan acento, deben colar.

    Comparar bytes no sólo evita el 500 — hace que una credencial no-ASCII
    legítima funcione, cosa que con `str` era imposible.
    """
    monkeypatch.setattr(auth_router, "REVIEW_USER", "revisör")
    monkeypatch.setattr(auth_router, "REVIEW_PASSWORD", "cläve-secreta")
    monkeypatch.setattr(auth_router, "REVIEW_STEAM_ID", STEAM_ID)

    resp = client.post("/auth/review-login", json={"user": "revisör", "password": "cläve-secreta"})

    assert resp.status_code == 200
    assert _decode(resp.json()["access_token"])["sub"] == STEAM_ID


def test_review_login_rejects_non_string_credentials(client, review_configured):
    """Un JSON con tipos raros no puede tumbar el endpoint con un 500."""
    resp = client.post("/auth/review-login", json={"user": 12345, "password": ["lista"]})

    assert resp.status_code == 401


# ── SEC-01: el flag Secure de la cookie de refresh sale de COOKIE_SECURE ──────

@pytest.mark.parametrize("secure", [True, False])
def test_refresh_cookie_secure_flag_follows_the_setting(client, monkeypatch, secure):
    """El flag no está hardcodeado: sigue a COOKIE_SECURE en los dos sentidos.

    `_set_refresh_cookie` vive en `auth.service`, que importa COOKIE_SECURE por
    valor — de ahí el setattr sobre ese módulo y no sobre `settings`.
    """
    monkeypatch.setattr(auth_service, "COOKIE_SECURE", secure)

    resp = client.post("/auth/token", json={"code": _seed_code()})

    assert resp.status_code == 200
    assert ("secure" in resp.headers["set-cookie"].lower()) is secure


@pytest.mark.parametrize("secure", [True, False])
def test_logout_cookie_secure_flag_follows_the_setting(client, monkeypatch, secure):
    """El logout borra la cookie con los mismos atributos con que se emitió.

    Si no coinciden, el navegador no la considera la misma cookie y el borrado
    no surte efecto.
    """
    monkeypatch.setattr(auth_router, "COOKIE_SECURE", secure)

    resp = client.post("/auth/logout")

    assert resp.status_code == 200
    assert ("secure" in resp.headers["set-cookie"].lower()) is secure


def test_cookie_secure_defaults_to_true_when_unset(monkeypatch):
    """El invariante de SEC-01: sin la env var definida, Secure=True.

    Un despliegue que olvide configurarla tiene que fallar hacia "no funciona
    en local por HTTP", nunca hacia "va inseguro en producción".

    Hay que neutralizar también el `.env`: `settings` llama a `load_dotenv()` al
    importarse, y el `.env` de desarrollo trae `COOKIE_SECURE=false`. Sin esto
    el test mediría el `.env` del worktree en vez del default del código, que es
    justo lo que un despliegue sin la variable ejercitaría.
    """
    import importlib

    import dotenv

    import settings

    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(settings, "load_dotenv", lambda *a, **k: False)
    reloaded = importlib.reload(settings)

    try:
        assert reloaded.COOKIE_SECURE is True
    finally:
        monkeypatch.undo()
        importlib.reload(settings)


@pytest.mark.parametrize("valor", ["false", "False", "0", "no", "NO"])
def test_cookie_secure_accepts_the_documented_falsy_values(monkeypatch, valor):
    """Los valores que apagan el flag son exactamente los documentados."""
    import importlib

    import settings

    monkeypatch.setenv("COOKIE_SECURE", valor)
    reloaded = importlib.reload(settings)

    try:
        assert reloaded.COOKIE_SECURE is False
    finally:
        importlib.reload(settings)


# ── SEC-06: cliente nativo (Origin https://localhost) recibe el refresh en el cuerpo ──
#
# El WebView de Capacitor pide desde https://localhost, cross-site respecto a la
# API, y Chromium rechaza un Set-Cookie SameSite=Strict en esa situación. Para
# ese origen el refresh viaja en el JSON y se acepta de vuelta en el cuerpo de
# /auth/refresh y /auth/logout. La web sigue con cookie HttpOnly y nunca ve el
# refresh en el cuerpo.

NATIVE = {"Origin": "https://localhost"}


def test_native_token_exchange_returns_refresh_in_body_and_no_cookie(client):
    resp = client.post("/auth/token", json={"code": _seed_code()}, headers=NATIVE)

    assert resp.status_code == 200
    assert _decode(resp.json()["refresh_token"])["type"] == "refresh"
    assert "refresh_token" not in resp.cookies


def test_web_token_exchange_never_puts_the_refresh_in_the_body(client):
    resp = client.post("/auth/token", json={"code": _seed_code()})
    assert "refresh_token" not in resp.json()

    spoof = client.post("/auth/token", json={"code": _seed_code("otro")}, headers={"Origin": "https://evil.example"})
    assert "refresh_token" not in spoof.json()


def test_native_refresh_from_body_rotates_and_answers_in_body(client):
    old_refresh = client.post("/auth/token", json={"code": _seed_code()}, headers=NATIVE).json()["refresh_token"]
    old_jti = _decode(old_refresh)["jti"]

    resp = client.post("/auth/refresh", json={"refresh_token": old_refresh}, headers=NATIVE)

    assert resp.status_code == 200
    new_refresh = resp.json()["refresh_token"]
    assert _decode(new_refresh)["jti"] != old_jti
    assert old_jti not in _refresh_store
    assert "refresh_token" not in resp.cookies
    assert client.post("/auth/refresh", json={"refresh_token": old_refresh}, headers=NATIVE).status_code == 401


def test_native_logout_from_body_revokes_the_jti(client):
    refresh = client.post("/auth/token", json={"code": _seed_code()}, headers=NATIVE).json()["refresh_token"]
    jti = _decode(refresh)["jti"]

    assert client.post("/auth/logout", json={"refresh_token": refresh}, headers=NATIVE).status_code == 200
    assert jti not in _refresh_store


def test_refresh_with_empty_or_malformed_body_is_a_401_not_a_500(client):
    assert client.post("/auth/refresh", headers=NATIVE).status_code == 401
    assert client.post("/auth/refresh", content=b"no es json", headers={**NATIVE, "Content-Type": "application/json"}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": 123}, headers=NATIVE).status_code == 401


def test_cookie_wins_over_body_when_both_are_present(client):
    """Un cuerpo con basura no debe romper el flujo web con cookie válida."""
    refresh = _login(client)
    resp = client.post("/auth/refresh", json={"refresh_token": "basura"}, cookies={"refresh_token": refresh})
    assert resp.status_code == 200
    assert "refresh_token" in resp.cookies


def test_review_login_and_dev_token_follow_the_same_native_rule(client, review_configured, monkeypatch):
    monkeypatch.setattr(auth_router, "DEV_TOKEN_ENABLED", True)

    review = client.post("/auth/review-login", json={"user": "revisor", "password": "clave-secreta"}, headers=NATIVE)
    assert review.status_code == 200
    assert "refresh_token" in review.json() and "refresh_token" not in review.cookies

    dev = client.post("/auth/dev-token", json={"steam_id": STEAM_ID}, headers=NATIVE)
    assert dev.status_code == 200
    assert "refresh_token" in dev.json() and "refresh_token" not in dev.cookies
