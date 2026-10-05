from dotenv import load_dotenv
import os

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:4200")
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-secret")
STEAM_GAME = os.getenv("STEAM_GAME", "cs2")

# Flag `Secure` de la cookie de refresh (SEC-01). Default `true` a propósito:
# si alguien olvida la variable, el fallo es "no funciona en local por HTTP",
# no "va inseguro en producción" — el error cae del lado seguro.
# En dev local se pone COOKIE_SECURE=false en el .env.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() not in ("false", "0", "no")

# Entorno de ejecución y modo debug (SEC-02).
ENV = os.getenv("ENV", "development")  # development | production
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# POST /auth/dev-token emite tokens para cualquier SteamID sin pasar por Steam.
# Exige AMBAS cosas: DEBUG activo Y no estar en producción. Con ENV=production
# fijo en Render, el endpoint queda muerto pase lo que pase con DEBUG.
DEV_TOKEN_ENABLED = DEBUG and ENV != "production"

# Supabase: histórico persistente del índice de precio CS2.
# El backend usa la service_role key (bypassa RLS) — nunca la anon/publishable.
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# Leetify (SEC-09): la clave de la API pública vive SOLO en el backend. El frontend
# la incrustaba en el bundle; ahora llama a /me/stats y nunca la ve.
LEETIFY_API_KEY = os.getenv("LEETIFY_API_KEY", "")

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

# Interruptor de Sharky (PERF-04). `false` deja /rag/chat devolviendo 404 y el
# frontend esconde la UI del chat: es el mismo flag por los dos lados.
#
# Existe porque la cuota del free tier de Gemini (20 req/día por proyecto) no da
# para servir a usuarios reales, y la idea es que el chat forme parte de un plan
# de suscripción. Poder apagarlo y encenderlo sin tocar código es el requisito.
CHAT_ENABLED = os.getenv("CHAT_ENABLED", "true").lower() not in ("false", "0", "no")

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

# Antigüedad máxima de una fuente citada en /rag/chat (UX-15, red de seguridad).
# La similitud no distingue un changelog de 2023 de uno de la semana pasada, y un
# artículo viejo bajo una predicción de precio es lo que rompe la confianza en las
# citas. 0 desactiva el filtro. Solo afecta a `sources[]`, no al contexto del modelo.
RAG_SOURCE_MAX_AGE_DAYS = int(os.getenv("RAG_SOURCE_MAX_AGE_DAYS", "365"))

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

# Lookups a steamwebapi /item que el price-tick puede gastar AL DÍA (PERF-11).
# Es el mando del gasto: ampliar el plan de steamwebapi = subir esta variable en
# Render, sin tocar código.
#
# Cada skin capturada cuesta 1 request. Sin tope, el tick hacía una por skin
# seguida (~800/día) y el plan Starter da 10 000/mes ≈ 333/día para TODO el
# backend: la cuota se agotaba hacia el día 13 del ciclo, dos meses seguidos.
# Cómo dimensionarlo: cuota_mensual / 31, menos lo que gastan el alerts-tick,
# la creación de alertas y el /market/price del sheet. Starter: 250 (≈7 750/mes,
# deja ~2 250 de margen).
#
# Si la población pendiente no cabe, entra por prioridad (alerta activa →
# inventario → trending; vista price_tick_queue) y el resto sale en la
# respuesta como `fuera_de_presupuesto`.
#
# Techo práctico sin tocar más que esta variable: el limiter (18/60 s) y el
# timeout del job de GitHub (350 min) dan ~6 000 skins/día.
PRICE_DAILY_BUDGET = int(os.getenv("PRICE_DAILY_BUDGET", "250"))

# Cuántos items del trending se registran en tracked_skins en cada captura.
# Sin esto, los items del ranking no tienen serie propia y toda predicción sobre
# ellos cae a CSFloat (predict/service.py). Se cogen los N primeros por turnover
# (precio × volumen 24h), que es el orden con el que ya se sirve la lista.
# Re-registrar cada hora no pisa `first_seen` ni `last_captured`: solo refresca
# `last_seen`, que es lo que las mantiene en la cola del price-tick.
# Ya no compiten con el inventario: el trending es la última prioridad del
# presupuesto diario (PRICE_DAILY_BUDGET) y cae de la cola a los 30 días sin
# volver a aparecer. Subirlo solo añade candidatas a la cola, no gasto.
TRENDING_TRACK_TOP = int(os.getenv("TRENDING_TRACK_TOP", "80"))

# Alertas de precio por skin (POST /internal/alerts-tick, cron horario).
ALERTS_TICK_TOKEN = os.getenv("ALERTS_TICK_TOKEN", "")
# Alertas evaluadas por tick. Cada skin distinta cuesta 1 req a steamwebapi por
# el mismo limiter que el price-tick (18/60 s): 18 = una ventana, ≤432 req/día
# con el cron horario. Las que no entran rotan al siguiente tick (LRU por
# last_checked_at).
ALERTS_LOOKUP_CAP = int(os.getenv("ALERTS_LOOKUP_CAP", "18"))
# PUSH-10: segundos entre ticks internos de alertas (0 = desactivado). El schedule de
# GitHub ejecuta alerts-tick.yml cada 3–9 h en vez de cada hora, así que el backend
# evalúa él mismo mientras el pinger (PERF-07) lo mantiene despierto; el workflow
# queda de respaldo. Cuesta cero cuota: los lookups a /item siguen siendo uno por
# skin y día (LOOKUP_MIN_INTERVAL); el resto son lecturas de Supabase.
ALERTS_TICK_INTERVAL = int(os.getenv("ALERTS_TICK_INTERVAL", "900"))
# PERF-14: reintento en segundo plano del inventario tras un 429 de steamwebapi (límite
# por minuto, transitorio; el 402 de cuota mensual NO se reintenta). Backoff exponencial
# con jitter: espera = max(Retry-After, BASE·2^intento) acotada a CAP, con ±50 % de jitter.
INVENTORY_429_MAX_RETRIES = int(os.getenv("INVENTORY_429_MAX_RETRIES", "4"))
INVENTORY_429_BACKOFF_BASE = float(os.getenv("INVENTORY_429_BACKOFF_BASE", "5"))   # s
INVENTORY_429_BACKOFF_CAP = float(os.getenv("INVENTORY_429_BACKOFF_CAP", "120"))   # s
# Tope de alertas activas por usuario: acota la cuota que un solo usuario
# puede consumir y el tamaño de la rueda.
ALERTS_MAX_PER_USER = int(os.getenv("ALERTS_MAX_PER_USER", "20"))
