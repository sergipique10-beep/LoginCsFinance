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
| Universo de skins | **Seed curado** (~150 líquidas) **∪** skins vistas en inventarios | La API Starter **no puede** dar "top-N por volumen" (ver Hallazgo API). Un seed curado garantiza cobertura desde el día 1; el inventario lo hace crecer con el uso. La predicción es sobre el `market_hash_name`, independiente del dueño. |
| Seed curado | Lista estática de ~150 skins icónicas/líquidas en el repo (`steam/data/tracked_seed.json`) | Estable, gratis, sin depender de un ranking de la API que no existe. Ampliable. |
| Cómo entran las de inventario | **Auto-registro**: al servir `/inventory`, registrar sus `market_hash_name` en `tracked_skins` (best-effort) | No hay tabla de usuarios; se llena solo con el uso real, sin iteración por-usuario. Un fallo de registro nunca rompe `/inventory`. |
| Fuente del precio | `GET {STEAM_WEB_API}/item?market_hash_name=<name>` (lookup exacto, 1 item/llamada) → precio canónico + `sold24h` | Verificado en vivo: exacto y con `sold24h` poblado para skins populares (p.ej. AK Redline FT: $43.15, 69 ventas). |
| Precio a guardar | Precio canónico (misma lógica que `_map_item`: `pricelatestsell`/`pricereal`) + `sold24h` como `volume` | Consistencia con lo que ve el usuario; `volume` queda para features de XGBoost. |
| Granularidad | 1 snapshot por `(market_hash_name, date)` (diario) | Horizonte de predicción semanal → diario sobra. Upsert idempotente. |
| Cron | GitHub Actions diario → `POST /internal/price-tick` (token) | Mismo patrón que `cap-tick`; despierta el Render dormido. |
| Rate limit | Lookup por-nombre vía `_SlidingWindowLimiter` (≤18/60s), **priorizando la menos-recientemente-capturada** y con **tope por corrida** (`PRICE_LOOKUP_CAP`) | Respeta 2k/día. Si `tracked > cap`, se cubren todas por rotación a lo largo de los días. |

## Arquitectura

```
Seed inicial (una vez, idempotente):
  steam/data/tracked_seed.json (~150 nombres) → register_tracked(names, 'top_n')
  (corre en el arranque si tracked_skins está vacía, o vía script)

Auto-registro (continuo):
  GET /inventory  → (además de lo que ya hace) registra los market_hash_name
                    en tracked_skins (source='inventory'), upsert idempotente,
                    best-effort (un fallo nunca rompe /inventory).

Captura diaria:
  GitHub Actions (cron diario)
    → POST /internal/price-tick   (X-Price-Tick-Token, secrets.compare_digest)
        → leer tracked_skins, ordenar por última captura (menos reciente primero)
        → tomar hasta PRICE_LOOKUP_CAP nombres
        → para cada uno: GET /item?market_hash_name=<name> vía _SlidingWindowLimiter
             → extraer precio canónico + sold24h
        → upsert (market_hash_name, date, price, volume, source) en precios_historicos
        → devolver {tracked, captured, skipped, errors}
```

## Hallazgo API (verificado en vivo 2026-07-17)

steamwebapi Starter **no ofrece "top-N por volumen"**: no ordena server-side
(ningún `sort`/`order`/`sortby` cambia el orden), y el orden default de `/items`
no es por popularidad (de los primeros 1000 items, solo 32 tienen ventas, y las
"más vendidas" que devuelve son stickers oscuros). Por eso el set de populares
es un **seed curado**, no un ranking de la API. En cambio, el lookup por-nombre
`/item?market_hash_name=<name>` **sí** devuelve el ítem exacto con `sold24h`
poblado para skins líquidas — es la fuente de precio de la captura.

### Módulos nuevos / tocados

- **`steam/price_history_repo.py`** (nuevo) — capa Supabase (patrón
  `cap_history_repo.py`: cliente cacheado service_role, `asyncio.to_thread`):
  - `register_tracked(names: list[str], source: str) -> None` — upsert en
    `tracked_skins` (no-op si vacío; no pisa `source`/`last_captured` existentes).
  - `fetch_tracked(limit: int) -> list[str]` — hasta `limit` nombres ordenados por
    `last_captured` ascendente con **nulls primero** (nunca-capturadas antes).
  - `upsert_prices(rows: list[dict]) -> None` — upsert por `(market_hash_name, date)`.
  - `mark_captured(names: list[str], date) -> None` — set `last_captured=date` para
    los nombres capturados (mueve la rotación).
  - `count_tracked() -> int` — para el seed idempotente (¿tabla vacía?).
- **`steam/data/tracked_seed.json`** (nuevo) — lista curada de ~150
  `market_hash_name` líquidos (AK/AWP/M4/cuchillos/guantes/cajas en wears
  comunes). Asset estático versionado.
- **`steam/price_capture.py`** (nuevo) — orquesta: `seed_tracked()` (lee el JSON
  y registra si la tabla está vacía) y `capture(client)` (lee tracked ordenadas
  por última captura, hasta `PRICE_LOOKUP_CAP`, lookup por-nombre vía limiter,
  extrae precio+volumen, upsert). Devuelve el dict de resultados. Depende de:
  `price_history_repo`, `steam/services` (limiter, `STEAM_WEB_API`), `settings`.
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
    first_seen       timestamptz not null default now(),
    last_captured    date                       -- null = nunca capturada (prioridad máxima)
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
| `PRICE_LOOKUP_CAP` | `400` | Tope de lookups por-nombre por corrida (protege los 2k/día). Si `tracked > cap`, se cubren por rotación (menos-recientemente-capturada primero). |

(Ya no hay `PRICE_TOP_N`: el set de populares es el seed curado `tracked_seed.json`, no un ranking de la API.)

## Cron

`.github/workflows/price-tick.yml` — 1×/día, `POST /internal/price-tick` con
`X-Price-Tick-Token`, espejando `cap-tick.yml` (env-var secrets
`BACKEND_BASE_URL` + `PRICE_TICK_TOKEN`, `curl -fsS --max-time`). Idempotente
(upsert por `(name, date)`).

## Tests (pytest, mocks)

- `register_tracked` / `fetch_tracked` / `upsert_prices` / `mark_captured` /
  `count_tracked` con Supabase mockeado (no-op en vacío, on_conflict correcto,
  orden `last_captured` nulls-first, límite aplicado).
- `seed_tracked`: registra desde el JSON solo si `count_tracked()==0` (idempotente).
- `price_capture.capture`: respeta `PRICE_LOOKUP_CAP` (toma N nombres); lookup
  por-nombre mockeado → extrae precio canónico + `sold24h`; arma filas `(name,
  date, price, volume)`; llama `mark_captured`; un error de una skin no aborta el
  resto (best-effort, cuenta en `errors`).
- `/internal/price-tick`: token faltante/incorrecto → 401; válido → corre y
  devuelve el dict.
- `/inventory` sigue funcionando aunque `register_tracked` falle (best-effort).

## Verificación en implementación

Ya resuelto contra la API viva (ver Hallazgo API): no hay top-N por volumen; la
fuente de precio es `GET {STEAM_WEB_API}/item?market_hash_name=<name>`. El
implementador debe confirmar los **nombres exactos de los campos** de precio en
la respuesta de `/item` (`pricelatestsell`, `pricereal`, `sold24h`) y mapear el
precio canónico con la misma prioridad que `_map_item`.

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
