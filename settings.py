from dotenv import load_dotenv
import os

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:4200")
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret")
STEAM_GAME = os.getenv("STEAM_GAME", "cs2")

# Supabase: histórico persistente del índice de precio CS2.
# El backend usa la service_role key (bypassa RLS) — nunca la anon/publishable.
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# Token que protege POST /internal/cap-tick (cron externo de GitHub Actions).
CAP_TICK_TOKEN = os.getenv("CAP_TICK_TOKEN", "")

# Credenciales de acceso de revisión para Google Play (sin pasar por Steam).
REVIEW_USER = os.getenv("REVIEW_USER", "")
REVIEW_PASSWORD = os.getenv("REVIEW_PASSWORD", "")
REVIEW_STEAM_ID = os.getenv("REVIEW_STEAM_ID", "")

# Firebase Admin SDK: envía push notifications (FCM) a Android e iOS.
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")

# Token que protege POST /internal/news-tick (cron externo de GitHub Actions).
NEWS_TICK_TOKEN = os.getenv("NEWS_TICK_TOKEN", "")

# Token que protege POST /internal/broadcast (anuncio manual, workflow_dispatch).
BROADCAST_TOKEN = os.getenv("BROADCAST_TOKEN", "")

# Precarga del contexto RAG en el system prompt de cada mensaje del chat.
# Cuesta una llamada de embedding por turno; con `false` el modelo sigue
# pudiendo pedir el contexto vía la tool `buscar_contexto_rag`.
CHAT_RAG_PRELOAD = os.getenv("CHAT_RAG_PRELOAD", "true").lower() not in ("false", "0", "no")

# Gemini (Google AI Studio) — chat del asistente Sharky (POST /rag/chat).
# La key vive SOLO en el backend; el frontend nunca la ve.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

# Modelo de embeddings de Gemini para el RAG (768 dims vía outputDimensionality).
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")

# Token que protege POST /internal/rag-ingest (cron externo de GitHub Actions).
RAG_INGEST_TOKEN = os.getenv("RAG_INGEST_TOKEN", "")

# Feeds RSS a ingestar para el RAG (URLs separadas por coma).
_raw_feeds = os.getenv("RAG_FEEDS", "https://blog.counter-strike.net/feed/")
RAG_FEEDS: list[str] = [u.strip() for u in _raw_feeds.split(",") if u.strip()]

# Similitud mínima (cosine, 0..1) para que un chunk recuperado cuente como
# fuente citable en /rag/chat. `retrieve` descarta lo que quede por debajo, así
# que un chunk irrelevante no llega ni al system prompt ni a `sources`.
# Ver spec: "nunca inventa".
RAG_MIN_SIMILARITY = float(os.getenv("RAG_MIN_SIMILARITY", "0.5"))

# Whitelist de orígenes de retorno permitidos tras la auth de Steam.
# Separar múltiples valores con coma en .env.
# Debe incluir la URL web y el scheme nativo de Android.
_raw_origins = os.getenv("ALLOWED_REDIRECT_ORIGINS", FRONTEND_URL)
ALLOWED_REDIRECT_ORIGINS: frozenset[str] = frozenset(
    o.strip() for o in _raw_origins.split(",") if o.strip()
)

# CORS origins: siempre incluye FRONTEND_URL y https://localhost (Capacitor WebView).
_raw_cors = os.getenv("ALLOWED_CORS_ORIGINS", FRONTEND_URL)
_cors_set = {o.strip() for o in _raw_cors.split(",") if o.strip()}
_cors_set.add("https://localhost")
ALLOWED_CORS_ORIGINS: list[str] = list(_cors_set)

# Captura de precios históricos por-skin (POST /internal/price-tick, cron diario).
PRICE_TICK_TOKEN = os.getenv("PRICE_TICK_TOKEN", "")
# Skins por LOTE, no por día: el workflow llama al tick varias veces seguidas
# hasta cubrir la población (ver price-tick.yml).
#
# El troceado no es una optimización, es obligatorio: Render free corta las
# peticiones HTTP largas por su cuenta, y ampliar el --max-time del curl no
# sirve porque quien cierra la conexión es el proxy de Render, no curl. Medido:
#     200 skins ≈ 11 min → OK          400 skins ≈ 22 min → 502
# El corte está entre ambos. 150 (~8 min) deja margen sin disparar el número de
# llamadas.
#
# Cada lote es una corrida completa e independiente: escribe sus puntos y marca
# `last_captured` antes de devolver. Si el lote 3 de 3 falla, los dos primeros
# ya están guardados — a diferencia de la corrida única, donde un 502 a los 20
# minutos tiraba el trabajo entero (medido: 0 puntos escritos ese día).
#
# `fetch_tracked` ordena por last_captured ascendente con nulls primero, así que
# la llamada N+1 continúa donde acabó la N sin repetir ni saltarse ninguna.
#
# El reloj lo fija _history_limiter (18/60s): (n/18)*60 s por lote.
PRICE_LOOKUP_CAP = int(os.getenv("PRICE_LOOKUP_CAP", "150"))

# Cuántos items del trending se registran en tracked_skins en cada captura.
# Sin esto, los items del ranking no tienen serie propia y toda predicción sobre
# ellos cae a CSFloat (predict/service.py). Se cogen los N primeros por turnover
# (precio × volumen 24h), que es el orden con el que ya se sirve la lista.
# El registro es un upsert con ignore_duplicates → re-registrar cada hora es
# un no-op barato y no pisa `first_seen` ni `last_captured`.
# 80 ≈ el margen que deja PRICE_LOOKUP_CAP sobre las ~320 skins ya seguidas.
TRENDING_TRACK_TOP = int(os.getenv("TRENDING_TRACK_TOP", "80"))
