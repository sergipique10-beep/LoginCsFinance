import re
import secrets
import time
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request, Depends, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.base import BaseHTTPMiddleware

from settings import BASE_URL, FRONTEND_URL, JWT_SECRET, ALLOWED_REDIRECT_ORIGINS, STEAM_API_KEY, STEAM_GAME

logger = logging.getLogger("uvicorn.error")

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
STEAM_WEB_API = "https://www.steamwebapi.com/steam/api"

NONCE_TTL = 300              # segundos que un nonce permanece válido
CODE_TTL = 30                # segundos que un auth code de un solo uso permanece válido
RATE_LIMIT_CALLS = 10        # peticiones máximas por ventana por IP
RATE_LIMIT_WINDOW = 60       # segundos

ACCESS_TOKEN_TTL = timedelta(minutes=30)
REFRESH_TOKEN_TTL = timedelta(days=7)

TOKEN_AUDIENCE = "cs-finance"

STEAM_CDN      = "https://community.cloudflare.steamstatic.com/economy/image/"
CS2_APP_ID     = 730
CS2_CONTEXT_ID = 2

# ── Stores en memoria ──────────────────────────────────────────────────────────
# ADVERTENCIA: estos stores son válidos únicamente para despliegues con un solo
# worker. En entornos multi-worker o multi-instancia deben reemplazarse por Redis.
# TODO: reemplazar _nonces, _auth_codes y _refresh_store por Redis con TTL nativo.

_nonces: dict[str, tuple[float, str]] = {}  # nonce → (issued_at, redirect_origin)
_rate_store: dict[str, list[float]] = defaultdict(list)
_auth_codes: dict[str, tuple[str, float]] = {}   # code → (steam_id, expires_at)
_refresh_store: dict[str, float] = {}             # jti → expires_at (monotonic)


# ── Middleware: cabeceras de seguridad ─────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains; preload"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' https://avatars.steamstatic.com; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        return response


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    if JWT_SECRET == "change-this-secret":
        logger.warning(
            "JWT_SECRET es el valor por defecto inseguro — "
            "define un secreto fuerte en .env"
        )
    if len(JWT_SECRET) < 32:
        logger.warning(
            "JWT_SECRET tiene menos de 32 caracteres — "
            "usa secrets.token_urlsafe(48) para generar un secreto seguro"
        )
    if not STEAM_API_KEY:
        logger.warning(
            "STEAM_API_KEY no está configurada — "
            "los endpoints de Steam Web API no funcionarán"
        )
    app.state.http_client = httpx.AsyncClient(timeout=10.0)
    yield
    await app.state.http_client.aclose()


# ── App + middleware ───────────────────────────────────────────────────────────

app = FastAPI(title="Steam Login", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
app.add_middleware(SecurityHeadersMiddleware)


# ── Helpers internos ───────────────────────────────────────────────────────────

def _get_client_ip(request: Request) -> str:
    """Devuelve la IP real del cliente respetando proxies de confianza.

    En producción el proxy inverso (nginx, Caddy…) inyecta X-Forwarded-For.
    Se toma únicamente el primer valor de la cadena para evitar spoofing.
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
    """Consume el nonce y devuelve el redirect_origin asociado, o None si inválido/expirado."""
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
    """Genera un par (access_token, refresh_token) para el steam_id dado.

    El access_token lleva type="access" y expira en ACCESS_TOKEN_TTL.
    El refresh_token lleva type="refresh", un jti único y expira en REFRESH_TOKEN_TTL.
    El jti del refresh_token queda registrado en _refresh_store para poder revocarlo.
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

    # Registrar el jti en el store usando tiempo monotónico para la limpieza lazy
    _refresh_store[jti] = time.monotonic() + REFRESH_TOKEN_TTL.total_seconds()

    return access_token, refresh_token


def _set_refresh_cookie(response: JSONResponse, refresh_token: str) -> None:
    """Adjunta la cookie HttpOnly que transporta el refresh token."""
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=False,  # TODO prod: cambiar a True (ver CLAUDE.md § Pendiente para producción)
        samesite="strict",
        max_age=int(REFRESH_TOKEN_TTL.total_seconds()),
        path="/",  # "/" porque el proxy Angular reescribe /api/auth/* → /auth/*
    )


# ── Dependencia JWT (rutas protegidas) ─────────────────────────────────────────

_bearer = HTTPBearer()


def require_jwt(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """Valida el Bearer token y garantiza que sea de tipo 'access'.

    Rechaza explícitamente refresh tokens presentados como access tokens,
    evitando que un token robado de la cookie sirva para llamadas a la API.
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


# ── Inventory mapper ──────────────────────────────────────────────────────────

def _tag(tags: list[dict], category: str) -> dict | None:
    """Devuelve el primer tag con la categoría indicada, o None."""
    return next((t for t in tags if t.get("category") == category), None)


def _slugify(text: str) -> str:
    """Convierte un string a slug kebab-case."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _map_desc(desc: dict) -> dict:
    """Mapea una descripción de la API de Steam a los campos ISkinCard disponibles.

    Solo incluye campos que Steam provee directamente. Los precios, float y
    paint index quedan fuera — deben enriquecerse desde una fuente externa.
    """
    tags         = desc.get("tags", [])
    rarity_tag   = _tag(tags, "Rarity")
    weapon_tag   = _tag(tags, "Weapon")
    quality_tag  = _tag(tags, "Quality")
    exterior_tag = _tag(tags, "Exterior")
    type_tag     = _tag(tags, "Type")

    name: str             = desc.get("name", "")
    market_hash_name: str = desc.get("market_hash_name", "")

    icon  = desc.get("icon_url_large") or desc.get("icon_url", "")
    image = f"{STEAM_CDN}{icon}" if icon else ""

    rarity       = rarity_tag["localized_tag_name"] if rarity_tag else "Base Grade"
    rarity_color = rarity_tag.get("color", "b0c3d9") if rarity_tag else "b0c3d9"

    trade_lock = desc.get("market_tradable_restriction")

    return {
        "classId":       desc.get("classid"),
        "instanceId":    desc.get("instanceid"),
        "name":          name,
        "slug":          _slugify(market_hash_name),
        "weaponType":    weapon_tag["localized_tag_name"] if weapon_tag else None,
        "itemName":      name.split(" | ")[0] if " | " in name else None,
        "itemType":      type_tag["localized_tag_name"] if type_tag else None,
        "image":         image,
        "rarity":        rarity,
        "rarityColor":   rarity_color,
        "borderColor":   rarity_color,
        "quality":       quality_tag["localized_tag_name"] if quality_tag else "Normal",
        "isStatTrak":    "StatTrak™" in name,
        "isSouvenir":    "Souvenir" in name,
        "isStar":        "★" in name,
        "exterior":      exterior_tag["localized_tag_name"] if exterior_tag else None,
        "marketable":    bool(desc.get("marketable", 1)),
        "tradable":      bool(desc.get("tradable", 1)),
        "tradeLockDays": trade_lock if isinstance(trade_lock, int) else None,
    }


def _map_item(asset: dict, desc: dict) -> dict:
    """Mapea un par (asset, description) al esquema ISkinCard completo.

    Fusiona los campos de descripción con el assetid y los stubs de precio/float
    que deberán enriquecerse desde una fuente externa (p.ej. csgotrader prices).
    """
    return {
        "id": asset["assetid"],
        **_map_desc(desc),
        "floatMin":       None,
        "floatMax":       None,
        "paintIndex":     None,
        "phase":          None,
        "priceLatest":    0,
        "priceSafe":      0,
        "priceMin":       0,
        "priceMax":       0,
        "priceDelta24h":  0,
        "priceDelta7d":   0,
        "priceDelta30d":  0,
        "priceReal":      None,
        "externalPrices": [],
        "sold24h":        0,
        "sold7d":         0,
        "sold30d":        0,
        "soldTotal":      0,
        "offerVolume":    0,
        "buyOrderVolume": 0,
        "buyOrderPrice":  0,
        "hoursToSold":    0,
        "steamUrl":       None,
    }


# ── Rutas ──────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/auth/steam", summary="Redirige al login de Steam")
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


@app.get("/auth/steam/callback", summary="Callback OpenID de Steam — emite auth code")
async def steam_callback(request: Request, nonce: str = ""):
    # 1. CSRF: verificar nonce y recuperar el redirect_origin sellado al inicio del flujo
    redirect_origin = _consume_nonce(nonce) if nonce else None
    if redirect_origin is None:
        raise HTTPException(status_code=400, detail="Invalid or expired nonce")

    query_params = dict(request.query_params)

    # 2. Replay: return_to debe apuntar a nuestro propio callback
    return_to = query_params.get("openid.return_to", "")
    if not return_to.startswith(f"{BASE_URL}/auth/steam/callback"):
        raise HTTPException(status_code=400, detail="Tampered return_to URL")

    # 3. Verificar con Steam
    validation_params = {**query_params, "openid.mode": "check_authentication"}
    try:
        resp = await request.app.state.http_client.post(STEAM_OPENID_URL, data=validation_params)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Steam validation timed out")
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Could not reach Steam servers")

    if "is_valid:true" not in resp.text:
        raise HTTPException(status_code=401, detail="Steam authentication failed")

    # 4. Extraer y validar Steam ID (exactamente 17 dígitos)
    claimed_id = query_params.get("openid.claimed_id", "")
    match = re.search(r"/openid/id/(\d{17})$", claimed_id)
    if not match:
        raise HTTPException(status_code=400, detail="Could not parse Steam ID")

    steam_id = match.group(1)

    # 5. Emitir auth code de un solo uso (TTL CODE_TTL segundos)
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = (steam_id, time.monotonic() + CODE_TTL)

    return RedirectResponse(url=f"{redirect_origin}/auth/callback?code={code}")


@app.post("/auth/token", summary="Canjea el auth code por access token + refresh cookie")
async def exchange_token(request: Request):
    _rate_limit(_get_client_ip(request))

    body = await request.json()
    code: str = body.get("code", "")

    if not code:
        raise HTTPException(status_code=400, detail="Missing code")

    # Consumir el código (operación atómica: leer + borrar)
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


@app.post("/auth/refresh", summary="Rota el refresh token y devuelve nuevo access token")
async def refresh_tokens(
    request: Request,
    refresh_token: str | None = Cookie(default=None),
):
    _rate_limit(_get_client_ip(request))

    if not refresh_token:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    # Limpieza lazy: purgar JTIs expirados antes de operar sobre el store
    now_mono = time.monotonic()
    expired_jtis = [jti for jti, exp in _refresh_store.items() if now_mono > exp]
    for jti in expired_jtis:
        del _refresh_store[jti]

    # Decodificar y validar el refresh token
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
        # El JTI no existe: nunca fue válido, ya fue rotado o fue revocado
        raise HTTPException(status_code=401, detail="Refresh token revoked or reused")

    steam_id: str = payload["sub"]

    # Revocar el JTI anterior (rotación: cada refresh_token es de un solo uso)
    del _refresh_store[jti]

    access_token, new_refresh_token = _issue_tokens(steam_id)

    response = JSONResponse({"access_token": access_token})
    _set_refresh_cookie(response, new_refresh_token)
    return response


@app.post("/auth/logout", summary="Revoca el refresh token y limpia la cookie")
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
            # Token inválido o expirado: no hay JTI que revocar, continuar igualmente
            pass

    response = JSONResponse({"message": "Logged out"})
    response.delete_cookie(
        key="refresh_token",
        path="/",
        httponly=True,
        secure=False,  # TODO prod: cambiar a True (ver CLAUDE.md § Pendiente para producción)
        samesite="strict",
    )
    return response


@app.get("/me", summary="Info del usuario autenticado")
def get_me(user: dict = Depends(require_jwt)):
    return {"steam_id": user["sub"]}


@app.get("/inventory/{steam_id}", summary="Inventario CS2 del usuario")
async def get_inventory(
    steam_id: str,
    _: dict = Depends(require_jwt),
):
    if not re.fullmatch(r"\d{17}", steam_id):
        raise HTTPException(status_code=400, detail="Invalid Steam ID format")

    url = (
        f"{STEAM_WEB_API}/inventory/?steam_id={steam_id}&game={STEAM_GAME}&key={STEAM_API_KEY}"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.steamwebapi.com",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url, params={"l": "english", "count": 5000}, headers=headers
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Steam inventory request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="Inventory is private")
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Steam rate limit — retry later")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()

    if not data.get("success"):
        raise HTTPException(status_code=502, detail="Steam returned an unsuccessful response")

    assets       = data.get("assets", [])
    descriptions = data.get("descriptions", [])

    # Indexar descriptions por (classid, instanceid) para lookup O(1)
    desc_index: dict[tuple[str, str], dict] = {
        (d["classid"], d["instanceid"]): d
        for d in descriptions
    }

    items = []
    for asset in assets:
        desc = desc_index.get((asset["classid"], asset["instanceid"]))
        if desc:
            items.append(_map_item(asset, desc))

    return items


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=True)
