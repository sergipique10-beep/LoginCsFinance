# Captura de precios históricos por-skin — Diseño

**Fecha:** 2026-07-17
**Rama:** `feat/rag-chat`
**Alcance:** Sub-proyecto 1 de la parte numérica/predictiva del chat Sharky.
**Explícitamente fuera de alcance:** entrenamiento del modelo, endpoint de
predicción, orquestador con function calling, tool de precio actual. Ver
"Fuera de alcance".

## Contexto y problema

El chat Sharky necesita, en fase 2, poder responder "¿va a subir esta skin la
próxima semana?". Eso requiere un **modelo de predicción**, que a su vez
requiere una **serie temporal de precios por-skin**. Hoy esa serie **no existe**:

- steamwebapi **no devuelve histórico pasado** (`history: []`).
- `market_cap_history` (Supabase) es el **índice agregado** del mercado CS2 (4
  métricas globales por hora), **no** precios por skin.
- El backend **no persiste usuarios ni skins por usuario** (tablas actuales:
  `market_cap_history`, `device_tokens`, `notified_news`).

Por lo tanto no se puede entrenar nada todavía. Este sub-proyecto **no entrena
ni predice**: su único objetivo es **empezar a acumular la serie temporal
por-skin desde ya**, para que en unas semanas/meses haya datos con los que
entrenar. Cada día sin capturar es un día de datos perdido.

### Estado del repo relevante (auditoría 2026-07-17)

- Patrón consolidado **cron externo (GitHub Actions) → endpoint interno con
  token → Supabase**: `cap-tick` ([.github/workflows/cap-tick.yml](../../../.github/workflows/cap-tick.yml),
  `POST /internal/cap-tick`), `news-tick`.
- Capa de datos Supabase sync envuelta en `asyncio.to_thread`:
  [steam/cap_history_repo.py](../../../steam/cap_history_repo.py).
- Precio canónico por skin: `_map_item` ([steam/mappers.py](../../../steam/mappers.py))
  produce `priceLatest` (desde `pricelatestsell` con fallbacks) y `sold24h`.
- Fuente bulk: `GET {STEAM_WEB_API}/items` (usado por `/market/items`) devuelve
  listas de items con precios (`pricelatestsell`, `sold24h`, …).
- Rate limit steamwebapi Starter: **20 req/60s por endpoint, 2.000/día**. Existe
  un `_SlidingWindowLimiter` (`_history_limiter`, ≤18/60s) en
  [steam/services.py](../../../steam/services.py).
- `/inventory` ([steam/routes/items.py](../../../steam/routes/items.py)) ya
  corre por usuario y devuelve items mapeados con su `market_hash_name`.

## Decisiones de diseño

| Decisión | Elección | Motivo |
|----------|----------|--------|
| Universo de skins | Top-N populares (set fijo) **∪** skins vistas en inventarios | Serie continua para un modelo general + cobertura de lo que los usuarios tienen. La predicción es sobre el `market_hash_name` (tipo de skin), independiente del dueño. |
| Cómo entran las de inventario | **Auto-registro**: al servir `/inventory`, registrar sus `market_hash_name` en `tracked_skins` | No hay tabla de usuarios; se llena solo con el uso real, sin fricción ni iteración por-usuario. |
| Precio a guardar | `priceLatest` de `_map_item` (canónico, el que ve el usuario) + `sold24h` como `volume` | No inventar otra fuente; consistencia con la app. `volume` queda para features de XGBoost. |
| Granularidad | 1 snapshot por `(market_hash_name, date)` (diario) | Horizonte de predicción semanal → diario sobra. Upsert idempotente. |
| Cron | GitHub Actions diario → `POST /internal/price-tick` (token) | Mismo patrón que `cap-tick`; despierta el Render dormido. |
| Rate limit | Bulk primero; nombres seguidos ausentes del bulk → lookup por-nombre con `_SlidingWindowLimiter`, **con tope por corrida** | Respeta 2k/día. |
| Top-N inicial | ~250, configurable (`PRICE_TOP_N`) | Cabe en el rate limit; buena cobertura de las skins líquidas. |

## Arquitectura

```
Auto-registro (continuo):
  GET /inventory  → (además de lo que ya hace) registra los market_hash_name
                    en tracked_skins (source='inventory'), upsert idempotente.

Seed inicial (una vez / al arrancar):
  fetch bulk de items populares por volumen  → tracked_skins (source='top_n')

Captura diaria:
  GitHub Actions (cron diario)
    → POST /internal/price-tick   (X-Price-Tick-Token, secrets.compare_digest)
        → leer tracked_skins
        → fetch bulk de precios (pocas llamadas) → dict {name: (price, volume)}
        → para cada tracked skin:
             - si está en el bulk → usar ese precio
             - si no → lookup por-nombre vía _SlidingWindowLimiter (con tope)
        → upsert (market_hash_name, date, price, volume, source) en precios_historicos
        → devolver {tracked, captured, from_bulk, from_lookup, skipped}
```

### Módulos nuevos / tocados

- **`steam/price_history_repo.py`** (nuevo) — capa Supabase (patrón
  `cap_history_repo.py`: cliente cacheado service_role, `asyncio.to_thread`):
  - `register_tracked(names: list[str], source: str) -> None` — upsert en
    `tracked_skins` (no-op si vacío).
  - `fetch_tracked() -> list[str]` — todos los `market_hash_name` seguidos.
  - `upsert_prices(rows: list[dict]) -> None` — upsert por `(market_hash_name, date)`.
- **`steam/price_capture.py`** (nuevo) — orquesta la captura diaria: bulk fetch,
  cruce con tracked, fallback por-nombre limitado, armado de filas, upsert.
  Devuelve el dict de resultados. Depende de: `price_history_repo`,
  `steam/services` (limiter), `steam/mappers` (`_map_item`/precio), `settings`.
- **`steam/routes/market.py`** o un router nuevo — `POST /internal/price-tick`
  (token). Registrar en `main.py` si es router nuevo.
- **`steam/routes/items.py`** (`/inventory`) — tras mapear el inventario,
  llamar `register_tracked([...], 'inventory')` de forma no bloqueante
  (best-effort; un fallo de registro nunca debe romper `/inventory`).

### Esquema SQL (Supabase, proyecto cs-finance)

```sql
create table if not exists public.tracked_skins (
    market_hash_name text primary key,
    source           text not null,            -- 'top_n' | 'inventory'
    first_seen       timestamptz not null default now()
);
alter table public.tracked_skins enable row level security;

create table if not exists public.precios_historicos (
    id               bigint generated always as identity primary key,
    market_hash_name text    not null,
    date             date    not null,
    price            numeric not null,
    volume           int,
    source           text,
    created_at       timestamptz not null default now(),
    unique (market_hash_name, date)
);
create index if not exists precios_historicos_name_date_idx
    on public.precios_historicos (market_hash_name, date);
alter table public.precios_historicos enable row level security;
```
RLS habilitado sin policies: el backend usa `service_role` (bypassa RLS), igual
que `market_cap_history`.

## Variables de entorno nuevas

| Variable | Default | Notas |
|----------|---------|-------|
| `PRICE_TICK_TOKEN` | *(vacío)* | Protege `POST /internal/price-tick`. Igual en GitHub Actions. Startup warns si falta. |
| `PRICE_TOP_N` | `250` | Tamaño del set fijo de populares a seguir. |
| `PRICE_LOOKUP_CAP` | `300` | Tope de lookups por-nombre por corrida (protege los 2k/día). |

## Cron

`.github/workflows/price-tick.yml` — 1×/día, `POST /internal/price-tick` con
`X-Price-Tick-Token`, espejando `cap-tick.yml` (env-var secrets
`BACKEND_BASE_URL` + `PRICE_TICK_TOKEN`, `curl -fsS --max-time`). Idempotente
(upsert por `(name, date)`).

## Tests (pytest, mocks)

- `register_tracked` / `fetch_tracked` / `upsert_prices` con Supabase mockeado
  (no-op en vacío, upsert con on_conflict correcto).
- `price_capture`: bulk cubre a una skin (sin lookup), otra ausente del bulk va
  al lookup; el tope `PRICE_LOOKUP_CAP` corta; armado de filas `(name, date,
  price, volume)`; el limiter se respeta (mockeado).
- `/internal/price-tick`: token faltante/incorrecto → 401; válido → corre y
  devuelve el dict.
- `/inventory` sigue funcionando aunque `register_tracked` falle (best-effort).

## Verificación en implementación

Un único punto a confirmar contra la API viva (no afecta la arquitectura): los
params exactos de `{STEAM_WEB_API}/items` para traer el **top-N por volumen**
(steamwebapi cambia nombres de params: `sort`/`order`/`sortby`…). El plan debe
incluir un paso de exploración con la API real antes de fijar la llamada.

## Fuera de alcance

- **Modelo de predicción (Prophet/XGBoost)** y su entrenamiento: sub-proyecto
  siguiente, cuando `precios_historicos` tenga semanas de datos.
- **Endpoint de predicción** (`predecir_precio_skin`).
- **Tool de precio actual** (`consultar_precio_actual`) y **orquestador con
  Gemini function calling**: sub-proyecto 2 (numérico/agente), independiente.
- Features avanzadas (rareza, float, eventos del juego): se suman al pasar de
  Prophet a XGBoost.

## Restricciones respetadas

- 100% free tier: Supabase free, GitHub Actions, steamwebapi Starter (respetando
  el rate limit con el limiter + tope). Sin servicios de pago.
- Números exactos por SQL/consulta directa, nunca por RAG/embeddings.
- Commits locales sí; push y merge NO (instrucción del operador).
