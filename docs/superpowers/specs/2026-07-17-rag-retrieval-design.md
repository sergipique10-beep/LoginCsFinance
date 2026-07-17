# RAG de recuperación sobre noticias CS2 — Diseño

**Fecha:** 2026-07-17
**Rama:** `feat/rag-chat`
**Alcance:** Módulo 1 (RAG) del plan Sharky fase 2. Los Módulos 2 (ML predictivo)
y 3 (orquestador con function calling) quedan **fuera de alcance** — ver sección
"Fuera de alcance".

## Contexto y problema

CS-FINANCE tiene un asistente ("Sharky") que hoy es un chat plano contra Gemini
(`POST /rag/chat`, [rag/router.py](../../../rag/router.py)): manda el mensaje del
usuario a Gemini y devuelve la respuesta, **sin mirar ningún dato**. No puede
responder preguntas cualitativas como "¿por qué subió el precio del Karambit?"
porque no tiene acceso a noticias ni contexto textual.

Este diseño agrega **retrieval**: indexar noticias del sector CS2 y, ante una
pregunta, recuperar las más relevantes y pasárselas a Gemini como contexto, con
citación de fuentes.

### Estado del repo (auditoría 2026-07-17)

- Backend FastAPI 0.136 + Supabase (Postgres) + Steam OpenID + steamwebapi,
  deploy en Render. Rama `feat/rag-chat`.
- Gemini ya integrado vía **REST directo** (sin SDK), reutilizando el
  `httpx.AsyncClient` compartido de la app. Key solo en backend.
- `POST /rag/chat` existe (chat plano, fase 1). **No es RAG todavía.**
- **No hay corpus de texto persistido.** `/news/cs2` trae noticias en vivo de la
  Steam News API; solo se guarda `notified_news` (gids para dedup de push).
- Existe patrón consolidado de **cron externo (GitHub Actions) → endpoint
  interno protegido por token** (`cap-tick`, `news-tick`), con capa de datos
  Supabase sync envuelta en `asyncio.to_thread` ([steam/cap_history_repo.py](../../../steam/cap_history_repo.py)).

## Decisiones de diseño

| Decisión | Elección | Motivo |
|----------|----------|--------|
| Fuente de corpus | Steam News API + **RSS oficiales** (blog CS2) | Bajo mantenimiento; RSS estable vs. scraping HTML frágil. Sin sitios de skins por ahora. |
| Embeddings | `gemini-embedding-001`, `outputDimensionality=768` | Free tier (1.500 req/día). 768 dims sobra para corpus chico; ahorra storage/velocidad. |
| Índice vectorial | **hnsw** (`vector_cosine_ops`) | "Ponelo y olvidate": buen recall sin re-tuning de `lists` (a diferencia de ivfflat). |
| Endpoint de consulta | **nuevo** `POST /rag/ask` | No romper `/rag/chat`. `retrieval.py` queda reutilizable como tool `buscar_contexto_rag` de la fase 2. |
| Frecuencia de ingesta | diaria (GitHub Actions) | Las noticias no cambian tan rápido; cuida cuota de embeddings. |
| Búsqueda | función SQL RPC `match_rag_chunks` | El `<=>` (coseno) queda en Postgres; supabase-py llama `.rpc()` limpio. |

## Arquitectura

```
GitHub Actions (cron diario)
   → POST /internal/rag-ingest   (protegido por X-Rag-Ingest-Token)
        → fetch feeds (RSS oficiales + Steam News API)
        → dedup por external_id (guid/url)         ← idempotente
        → limpieza HTML → chunking (~500-800 tokens si el artículo es largo)
        → embedding SOLO de chunks nuevos (Gemini Embedding, 768)
        → upsert en Supabase (rag_chunks, pgvector)

Usuario pregunta (POST /rag/ask, require_jwt + rate limit)
   → embedding de la pregunta (Gemini Embedding, 768)
   → match_rag_chunks(embedding, top_k=5)           ← búsqueda pgvector vía RPC
   → armado de contexto (título + texto + url de cada chunk)
   → prompt: system + "Usá SOLO este contexto: {chunks}" + pregunta
   → Gemini generateContent
   → { reply, sources[] }
```

### Módulos (extendiendo `rag/`)

Unidades chicas y de propósito único, siguiendo el estilo del repo:

- **`rag/embeddings.py`** — cliente Gemini Embedding sobre el `httpx.AsyncClient`
  compartido (sin SDK). `embed_text(client, text) -> list[float]` (768).
  Depende de: `settings` (`GEMINI_API_KEY`, `GEMINI_EMBED_MODEL`).
- **`rag/repo.py`** — capa Supabase para `rag_chunks`. Patrón `cap_history_repo.py`
  (cliente cacheado module-level, service_role, `asyncio.to_thread`).
  `upsert_chunks(rows)`, `match_chunks(embedding, k)` (llama RPC),
  `seen_external_ids(ids)` (dedup). Depende de: `settings` + supabase.
- **`rag/ingest.py`** — orquesta la ingesta: fetch RSS + Steam News, limpieza
  (reutiliza `_clean_news_content` de [steam/mappers.py](../../../steam/mappers.py)),
  chunking, dedup contra `seen_external_ids`, embedding de nuevos, upsert.
  Depende de: `rag/embeddings`, `rag/repo`, `steam/mappers`, `settings`.
- **`rag/retrieval.py`** — `retrieve(client, query, k) -> list[Chunk]`: embeddea la
  query y llama `match_chunks`. **Pieza reutilizable** (futura tool
  `buscar_contexto_rag`). Depende de: `rag/embeddings`, `rag/repo`.
- **`rag/gemini.py`** — ya existe. Se agrega `generate_with_context(client, question,
  chunks, history) -> str` que arma el prompt con contexto y salvaguarda
  "no inventes fuera del contexto".
- **`rag/router.py`** — ya existe (`/rag/chat`). Se agregan:
  - `POST /rag/ask` (require_jwt + rate limit) → retrieval + generación.
  - `POST /internal/rag-ingest` (header `X-Rag-Ingest-Token`, `compare_digest`) → ingesta.

## Esquema SQL (Supabase, proyecto `cs-finance`)

```sql
create extension if not exists vector;

create table if not exists public.rag_chunks (
    id           bigint generated always as identity primary key,
    source       text        not null,           -- 'steam_news' | 'cs2_blog' | ...
    external_id  text        not null,            -- guid/url del artículo (dedup)
    chunk_index  int         not null default 0,  -- 0..N si el artículo se parte
    title        text,
    url          text,
    content      text        not null,            -- texto limpio del chunk
    published_at timestamptz,
    embedding    vector(768) not null,
    created_at   timestamptz not null default now(),
    unique (external_id, chunk_index)             -- idempotencia del ingestor
);

create index if not exists rag_chunks_embedding_idx
    on public.rag_chunks
    using hnsw (embedding vector_cosine_ops);

-- RLS habilitado sin policies: backend usa service_role (bypassa RLS), como market_cap_history.
alter table public.rag_chunks enable row level security;

create or replace function public.match_rag_chunks(
    query_embedding vector(768),
    match_count int default 5
)
returns table (
    id bigint, source text, title text, url text,
    content text, published_at timestamptz, similarity float
)
language sql stable as $$
    select c.id, c.source, c.title, c.url, c.content, c.published_at,
           1 - (c.embedding <=> query_embedding) as similarity
    from public.rag_chunks c
    order by c.embedding <=> query_embedding
    limit match_count;
$$;
```

## Contrato del endpoint

`POST /rag/ask` (Bearer JWT):

```json
// request
{ "question": "¿por qué subió el Karambit?" }

// response
{
  "reply": "El Karambit subió porque...",
  "sources": [
    { "title": "...", "url": "https://...", "published_at": "2026-07-15T..." }
  ]
}
```

Si no hay chunks relevantes (tabla vacía o similitud baja) → `reply` explica que
no hay noticias sobre el tema; `sources` vacío. **Nunca inventa.**

## Variables de entorno nuevas

| Variable | Default | Notas |
|----------|---------|-------|
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | Modelo de embeddings (768 dims vía `outputDimensionality`). |
| `RAG_INGEST_TOKEN` | *(vacío)* | Secreto compartido que protege `POST /internal/rag-ingest`. Igual que en GitHub Actions. Startup warns si falta. |
| `RAG_FEEDS` | *(RSS oficial CS2)* | URLs RSS separadas por coma. Agregar fuentes sin tocar código. |
| `GEMINI_API_KEY` | *(ya existe)* | Reutilizada para embeddings y generación. |

Configurar en: `.env` local, secrets de Render, secrets de GitHub Actions.

## Cron de ingesta

`.github/workflows/rag-ingest.yml` — 1×/día, `POST /internal/rag-ingest` con
header `X-Rag-Ingest-Token`. Idempotente (dedup por `external_id`). Solo embeddea
chunks nuevos → gasto de cuota casi nulo en corridas repetidas.

## Tests (pytest, `tests/`)

Unit sobre piezas puras, con fetch RSS y llamadas Gemini/Supabase **mockeadas**
(estilo del repo):
- chunking (artículo corto → 1 chunk; largo → N chunks)
- limpieza HTML
- dedup contra `seen_external_ids`
- armado del prompt de contexto (incluye salvaguarda "solo contexto")
- `/rag/ask` con retrieval mockeado: mapea chunks → `sources`; caso sin chunks.

Sin test de integración real contra Gemini.

## Startup validation

Se agrega warning si `RAG_INGEST_TOKEN` falta (la ingesta no funcionará), en
línea con los warnings existentes de `main.py`.

## Fuera de alcance

- **Módulo 2 (ML predictivo por-skin):** no hay datos históricos por-skin para
  entrenar. steamwebapi no devuelve histórico pasado; `market_cap_history` es el
  índice **agregado** del mercado, no precios por skin. Requiere un proyecto de
  recolección de datos previo (semanas).
- **Módulo 3 (orquestador con Gemini function calling):** tiene sentido con ≥2
  tools reales. `retrieval.py` queda listo para ser la tool `buscar_contexto_rag`.

## Restricciones respetadas

- Stack 100% free: Supabase free, Gemini free (LLM + embeddings), GitHub Actions
  para el cron. Sin API de Claude ni servicios de pago.
- RAG solo para **texto no estructurado** (noticias). Cálculos numéricos exactos
  seguirán yendo por SQL/pandas directo (fuera de este módulo).
