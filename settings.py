from dotenv import load_dotenv
import os

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "http://localhost:8001")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:4200")
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret")
STEAM_GAME = os.getenv("STEAM_GAME", "cs2")

# Whitelist de orígenes de retorno permitidos tras la auth de Steam.
# Separar múltiples valores con coma en .env.
# Debe incluir la URL web y el scheme nativo de Android.
_raw_origins = os.getenv("ALLOWED_REDIRECT_ORIGINS", FRONTEND_URL)
ALLOWED_REDIRECT_ORIGINS: frozenset[str] = frozenset(
    o.strip() for o in _raw_origins.split(",") if o.strip()
)
