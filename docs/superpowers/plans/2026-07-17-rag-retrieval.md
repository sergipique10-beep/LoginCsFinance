# RAG de recuperación sobre noticias CS2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dar a Sharky la capacidad de responder preguntas cualitativas sobre el mercado CS2 recuperando noticias relevantes (RSS oficiales + Steam News) desde pgvector y pasándolas a Gemini como contexto, con citación de fuentes.

**Architecture:** Ingesta diaria vía cron externo (GitHub Actions → `POST /internal/rag-ingest`) que fetchea feeds, limpia, chunkea, embeddea (Gemini Embedding 768) y hace upsert en `rag_chunks` (Supabase pgvector, índice hnsw). Consulta vía `POST /rag/ask` que embeddea la pregunta, busca top-k por similitud coseno (RPC `match_rag_chunks`) y genera la respuesta con Gemini. Módulos chicos dentro de `rag/`, siguiendo el patrón cron→endpoint-interno-con-token y la capa Supabase sync→`asyncio.to_thread` ya existentes en el repo.

**Tech Stack:** Python 3 / FastAPI, Supabase (Postgres + pgvector), Gemini REST (embeddings + generateContent, sin SDK), `feedparser` para RSS, `httpx.AsyncClient` compartido, pytest.

## Global Constraints

- **Commits locales sí; push y merge NO** (instrucción del operador). Cada tarea termina en un commit local en `feat/rag-chat`. **Nunca** `git push` ni `git merge`. El operador pusheará/mergeará después. Formato de mensaje: `feat(rag): ...`, y terminar el cuerpo con la línea `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Stack 100% free: Supabase free, Gemini free (embeddings `gemini-embedding-001` 1.500 req/día + generación), GitHub Actions para el cron. Sin API de Claude ni servicios de pago.
- Embeddings: `gemini-embedding-001` con `outputDimensionality=768`. La columna es `vector(768)`.
- Rama de trabajo: `feat/rag-chat` (ya activa en ambos repos).
- Gemini se llama por **REST** con `httpx.AsyncClient` compartido (`app.state.http_client` / `request.app.state.http_client`), header `x-goog-api-key`. Sin SDK de Google.
- Capa Supabase: cliente cacheado a nivel módulo con `service_role`, llamadas sync envueltas en `asyncio.to_thread` (patrón `steam/cap_history_repo.py`).
- Tests: fetch RSS y llamadas Gemini/Supabase **mockeadas**. Sin integración real. Correr con `venv\Scripts\python -m pytest` (el Python del sistema no tiene las deps).
- El texto de noticias se limpia reutilizando `_clean_news_content` de `steam/mappers.py` (pasar `max_chars` grande para no truncar).

---

## File Structure

- `docs/sql/rag_chunks.sql` — **crear**. SQL a correr a mano en el SQL editor de Supabase (extensión, tabla, índice hnsw, función RPC). No hay pipeline de migraciones en el repo.
- `settings.py` — **modificar**. Añadir `GEMINI_EMBED_MODEL`, `RAG_INGEST_TOKEN`, `RAG_FEEDS`.
- `main.py` — **modificar**. Warning de startup si falta `RAG_INGEST_TOKEN`.
- `rag/embeddings.py` — **crear**. Cliente Gemini Embedding.
- `rag/repo.py` — **crear**. Capa Supabase para `rag_chunks`.
- `rag/ingest.py` — **crear**. Fetch feeds + limpieza + chunking + dedup + embed + upsert.
- `rag/retrieval.py` — **crear**. Embedding de query + búsqueda por similitud.
- `rag/gemini.py` — **modificar**. Añadir `generate_with_context`.
- `rag/router.py` — **modificar**. Añadir `POST /rag/ask` y `POST /internal/rag-ingest`.
- `.github/workflows/rag-ingest.yml` — **crear**. Cron diario.
- `requirements.txt` — **modificar**. Añadir `feedparser`.
- `.env.example` — **modificar**. Documentar las variables nuevas.
- `tests/test_rag_embeddings.py`, `tests/test_rag_ingest.py`, `tests/test_rag_retrieval.py`, `tests/test_rag_gemini.py`, `tests/test_rag_router.py` — **crear**.

---

## Task 1: Esquema SQL + configuración

**Files:**
- Create: `docs/sql/rag_chunks.sql`
- Modify: `settings.py` (tras la línea `GEMINI_MODEL`, ~L37)
- Modify: `main.py` (bloque de warnings del lifespan, tras el de GEMINI_API_KEY ~L69)
- Modify: `.env.example`

**Interfaces:**
- Produces: `settings.GEMINI_EMBED_MODEL: str`, `settings.RAG_INGEST_TOKEN: str`, `settings.RAG_FEEDS: list[str]`. Tabla Supabase `public.rag_chunks` + función `public.match_rag_chunks(query_embedding vector(768), match_count int)`.

- [ ] **Step 1: Escribir el SQL de la migración**

Crear `docs/sql/rag_chunks.sql`:

```sql
-- RAG de noticias CS2 — correr en el SQL editor del proyecto Supabase `cs-finance`.
create extension if not exists vector;

create table if not exists public.rag_chunks (
    id           bigint generated always as identity primary key,
    source       text        not null,
    external_id  text        not null,
    chunk_index  int         not null default 0,
    title        text,
    url          text,
    content      text        not null,
    published_at timestamptz,
    embedding    vector(768) not null,
    created_at   timestamptz not null default now(),
    unique (external_id, chunk_index)
);

create index if not exists rag_chunks_embedding_idx
    on public.rag_chunks
    using hnsw (embedding vector_cosine_ops);

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

- [ ] **Step 2: Añadir variables a `settings.py`**

Tras la línea `GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")`:

```python
# Modelo de embeddings de Gemini para el RAG (768 dims vía outputDimensionality).
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")

# Token que protege POST /internal/rag-ingest (cron externo de GitHub Actions).
RAG_INGEST_TOKEN = os.getenv("RAG_INGEST_TOKEN", "")

# Feeds RSS a ingestar para el RAG (URLs separadas por coma).
_raw_feeds = os.getenv("RAG_FEEDS", "https://blog.counter-strike.net/index.php/feed/")
RAG_FEEDS: list[str] = [u.strip() for u in _raw_feeds.split(",") if u.strip()]
```

- [ ] **Step 3: Añadir warning de startup en `main.py`**

En el import de `settings` (L9-15) añadir `RAG_INGEST_TOKEN` a la lista importada. Luego, tras el bloque `if not GEMINI_API_KEY:` (~L69), añadir:

```python
    if not RAG_INGEST_TOKEN:
        logger.warning(
            "RAG_INGEST_TOKEN no está configurada — "
            "la ingesta de noticias del RAG (POST /internal/rag-ingest) no funcionará"
        )
```

- [ ] **Step 4: Documentar en `.env.example`**

Añadir (cerca de las líneas de GEMINI):

```
# RAG de noticias CS2
GEMINI_EMBED_MODEL=gemini-embedding-001
RAG_INGEST_TOKEN=
RAG_FEEDS=https://blog.counter-strike.net/index.php/feed/
```

- [ ] **Step 5: Verificar que la app arranca e importa sin error**

Run: `venv\Scripts\python -c "import settings, main; print(settings.GEMINI_EMBED_MODEL, settings.RAG_FEEDS)"`
Expected: imprime `gemini-embedding-001 ['https://blog.counter-strike.net/index.php/feed/']` sin trazas de error.

- [ ] **Step 6: Commit local (sin push)**

```bash
git add docs/sql/rag_chunks.sql settings.py main.py .env.example
git commit -m "feat(rag): esquema SQL rag_chunks + config (embeddings, ingest token, feeds)"
```

---

## Task 2: Cliente de embeddings Gemini (`rag/embeddings.py`)

**Files:**
- Create: `rag/embeddings.py`
- Create: `pytest.ini` (config de pytest-asyncio — primera tarea con tests async)
- Modify: `requirements.txt` (añadir `pytest-asyncio`)
- Test: `tests/test_rag_embeddings.py`

**Interfaces:**
- Consumes: `settings.GEMINI_API_KEY`, `settings.GEMINI_EMBED_MODEL`; `httpx.AsyncClient`.
- Produces: `async def embed_text(client: httpx.AsyncClient, text: str) -> list[float]` — devuelve 768 floats. Lanza `RuntimeError` si falta la key o la respuesta viene vacía; propaga errores httpx.

- [ ] **Step 0: Setup de tests async (pytest-asyncio)**

El repo no tenía tests async hasta ahora. Las Tasks 2-6 los usan (`@pytest.mark.asyncio`). Configurar una sola vez:

Añadir `pytest-asyncio==0.24.0` a `requirements.txt` e instalar:

Run: `venv\Scripts\pip install pytest-asyncio==0.24.0`
Expected: `Successfully installed pytest-asyncio-0.24.0`

Crear `pytest.ini` en la raíz del repo:

```ini
[pytest]
asyncio_mode = auto
```

(`asyncio_mode = auto` trata toda `async def test_*` como test async sin necesitar el marker en cada una; los markers explícitos del plan siguen funcionando.)

Verificar que la suite existente sigue verde con la config nueva:

Run: `venv\Scripts\python -m pytest tests/ -q`
Expected: todos los tests previos pasan (sin errores de colección por la config nueva).

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/test_rag_embeddings.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from rag import embeddings


@pytest.mark.asyncio
async def test_embed_text_returns_vector(monkeypatch):
    monkeypatch.setattr(embeddings, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(embeddings, "GEMINI_EMBED_MODEL", "gemini-embedding-001")

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"embedding": {"values": [0.1, 0.2, 0.3]}})
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)

    out = await embeddings.embed_text(client, "hola mundo")

    assert out == [0.1, 0.2, 0.3]
    # pide el modelo y dimensionalidad correctos
    _, kwargs = client.post.call_args
    assert kwargs["json"]["output_dimensionality"] == 768


@pytest.mark.asyncio
async def test_embed_text_raises_without_key(monkeypatch):
    monkeypatch.setattr(embeddings, "GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError):
        await embeddings.embed_text(MagicMock(), "x")
```

- [ ] **Step 2: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_rag_embeddings.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'rag.embeddings'` (o `AttributeError`).

- [ ] **Step 3: Implementar `rag/embeddings.py`**

```python
"""Cliente mínimo de embeddings de Gemini (REST) para el RAG.

Reutiliza el httpx.AsyncClient compartido de la app, sin SDK de Google —
mismo patrón que rag/gemini.py. Produce vectores de 768 dims.
"""
import httpx

from settings import GEMINI_API_KEY, GEMINI_EMBED_MODEL

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_EMBED_TIMEOUT = 30.0
_EMBED_DIMS = 768


async def embed_text(client: httpx.AsyncClient, text: str) -> list[float]:
    """Devuelve el embedding (768 floats) del texto vía Gemini Embedding API.

    Lanza RuntimeError si falta la key o la respuesta viene vacía; propaga
    los errores httpx (el llamador los traduce).
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY no configurada")

    url = f"{_GEMINI_BASE}/{GEMINI_EMBED_MODEL}:embedContent"
    body = {
        "model": f"models/{GEMINI_EMBED_MODEL}",
        "content": {"parts": [{"text": text}]},
        "output_dimensionality": _EMBED_DIMS,
    }
    resp = await client.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json=body,
        timeout=_EMBED_TIMEOUT,
    )
    resp.raise_for_status()

    values = resp.json().get("embedding", {}).get("values", [])
    if not values:
        raise RuntimeError("Gemini devolvió un embedding vacío")
    return values
```

- [ ] **Step 4: Correr el test para verlo pasar**

Run: `venv\Scripts\python -m pytest tests/test_rag_embeddings.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit local (sin push)**

```bash
git add rag/embeddings.py tests/test_rag_embeddings.py pytest.ini requirements.txt
git commit -m "feat(rag): cliente de embeddings Gemini (768 dims) + setup pytest-asyncio"
```

---

## Task 3: Capa Supabase (`rag/repo.py`)

**Files:**
- Create: `rag/repo.py`
- Test: `tests/test_rag_repo.py`

**Interfaces:**
- Consumes: `settings.SUPABASE_URL`, `settings.SUPABASE_SERVICE_KEY`; `supabase.create_client`.
- Produces:
  - `async def upsert_chunks(rows: list[dict]) -> None` — upsert por `(external_id, chunk_index)`.
  - `async def match_chunks(embedding: list[float], k: int = 5) -> list[dict]` — llama RPC `match_rag_chunks`, devuelve filas `{id, source, title, url, content, published_at, similarity}`.
  - `async def seen_external_ids(external_ids: list[str]) -> set[str]` — subconjunto de ids ya presentes (dedup).
  - `def get_supabase() -> Client`.

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/test_rag_repo.py`:

```python
import pytest
from unittest.mock import MagicMock

from rag import repo


@pytest.mark.asyncio
async def test_match_chunks_calls_rpc(monkeypatch):
    fake_resp = MagicMock(data=[{"id": 1, "similarity": 0.9, "content": "c",
                                 "source": "s", "title": "t", "url": "u",
                                 "published_at": None}])
    fake_client = MagicMock()
    fake_client.rpc.return_value.execute.return_value = fake_resp
    monkeypatch.setattr(repo, "get_supabase", lambda: fake_client)

    out = await repo.match_chunks([0.1, 0.2], k=3)

    assert out[0]["id"] == 1
    fake_client.rpc.assert_called_once_with(
        "match_rag_chunks", {"query_embedding": [0.1, 0.2], "match_count": 3}
    )


@pytest.mark.asyncio
async def test_seen_external_ids_returns_present_subset(monkeypatch):
    fake_resp = MagicMock(data=[{"external_id": "a"}, {"external_id": "a"}])
    fake_client = MagicMock()
    (fake_client.table.return_value.select.return_value
        .in_.return_value.execute.return_value) = fake_resp
    monkeypatch.setattr(repo, "get_supabase", lambda: fake_client)

    out = await repo.seen_external_ids(["a", "b"])

    assert out == {"a"}


@pytest.mark.asyncio
async def test_upsert_chunks_noop_on_empty(monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: fake_client)

    await repo.upsert_chunks([])

    fake_client.table.assert_not_called()
```

- [ ] **Step 2: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_rag_repo.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'rag.repo'`.

- [ ] **Step 3: Implementar `rag/repo.py`**

```python
"""Persistencia del corpus RAG (rag_chunks) en Supabase (pgvector).

supabase-py es síncrono → todas las llamadas se envuelven en asyncio.to_thread.
Cliente cacheado a nivel módulo con service_role (bypassa RLS), igual que
steam/cap_history_repo.py.
"""
import asyncio

from supabase import create_client, Client

from settings import SUPABASE_URL, SUPABASE_SERVICE_KEY

_TABLE = "rag_chunks"
_client: Client | None = None


def get_supabase() -> Client:
    global _client
    if _client is None:
        if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_SERVICE_KEY no configuradas — "
                "no se puede acceder al corpus RAG"
            )
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


async def upsert_chunks(rows: list[dict]) -> None:
    """Upsert de chunks por (external_id, chunk_index). No-op si rows está vacío."""
    if not rows:
        return

    def _do() -> None:
        (get_supabase().table(_TABLE)
            .upsert(rows, on_conflict="external_id,chunk_index")
            .execute())

    await asyncio.to_thread(_do)


async def match_chunks(embedding: list[float], k: int = 5) -> list[dict]:
    """Top-k chunks por similitud coseno vía la función RPC match_rag_chunks."""
    def _do() -> list[dict]:
        resp = get_supabase().rpc(
            "match_rag_chunks",
            {"query_embedding": embedding, "match_count": k},
        ).execute()
        return resp.data or []

    return await asyncio.to_thread(_do)


async def seen_external_ids(external_ids: list[str]) -> set[str]:
    """Subconjunto de external_ids que ya existen en la tabla (para dedup)."""
    if not external_ids:
        return set()

    def _do() -> set[str]:
        resp = (get_supabase().table(_TABLE)
                .select("external_id")
                .in_("external_id", external_ids)
                .execute())
        return {r["external_id"] for r in (resp.data or [])}

    return await asyncio.to_thread(_do)
```

- [ ] **Step 4: Correr el test para verlo pasar**

Run: `venv\Scripts\python -m pytest tests/test_rag_repo.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit local (sin push)**

```bash
git add rag/repo.py tests/test_rag_repo.py
git commit -m "feat(rag): capa Supabase rag_chunks (upsert/match/dedup)"
```

---

## Task 4: Ingesta de feeds (`rag/ingest.py`) + `feedparser`

**Files:**
- Create: `rag/ingest.py`
- Modify: `requirements.txt` (añadir `feedparser`)
- Test: `tests/test_rag_ingest.py`

**Interfaces:**
- Consumes: `rag.embeddings.embed_text`, `rag.repo.seen_external_ids`, `rag.repo.upsert_chunks`, `steam.mappers._clean_news_content`, `settings.RAG_FEEDS`; `feedparser`; `httpx.AsyncClient`.
- Produces:
  - `def chunk_text(text: str, max_chars: int = 2000) -> list[str]` — parte texto largo en trozos ≤ max_chars por límite de palabra; texto corto → 1 trozo; vacío → `[]`.
  - `def parse_feed_entries(feeds_xml: list[str]) -> list[dict]` — parsea XML RSS → items `{external_id, title, url, content, published_at, source}`.
  - `async def ingest(client: httpx.AsyncClient) -> dict` — fetch feeds, dedup, chunk, embed nuevos, upsert. Devuelve `{"fetched": N, "new": M, "chunks": C}`.

- [ ] **Step 1: Añadir `feedparser` a `requirements.txt` e instalar**

Añadir línea `feedparser==6.0.11` a `requirements.txt`.
Run: `venv\Scripts\pip install feedparser==6.0.11`
Expected: `Successfully installed feedparser-6.0.11 sgmllib3k-...`

- [ ] **Step 2: Escribir el test que falla**

Crear `tests/test_rag_ingest.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from rag import ingest

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Karambit sube</title>
    <link>https://blog.cs/karambit</link>
    <guid>https://blog.cs/karambit</guid>
    <description>El precio del Karambit subio por la nueva operacion.</description>
    <pubDate>Tue, 15 Jul 2026 10:00:00 +0000</pubDate>
  </item>
</channel></rss>"""


def test_chunk_text_short_is_single_chunk():
    assert ingest.chunk_text("hola mundo") == ["hola mundo"]


def test_chunk_text_empty_is_empty():
    assert ingest.chunk_text("   ") == []


def test_chunk_text_long_splits():
    text = "palabra " * 1000  # ~8000 chars
    chunks = ingest.chunk_text(text, max_chars=2000)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)


def test_parse_feed_entries_extracts_fields():
    items = ingest.parse_feed_entries([RSS])
    assert len(items) == 1
    it = items[0]
    assert it["external_id"] == "https://blog.cs/karambit"
    assert it["title"] == "Karambit sube"
    assert "Karambit" in it["content"]


@pytest.mark.asyncio
async def test_ingest_embeds_only_new(monkeypatch):
    monkeypatch.setattr(ingest, "RAG_FEEDS", ["http://feed"])
    # http_client.get devuelve el RSS
    resp = MagicMock(text=RSS, status_code=200)
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    # nada visto aun → el item es nuevo
    monkeypatch.setattr(ingest.repo, "seen_external_ids", AsyncMock(return_value=set()))
    upsert = AsyncMock()
    monkeypatch.setattr(ingest.repo, "upsert_chunks", upsert)
    monkeypatch.setattr(ingest.embeddings, "embed_text",
                        AsyncMock(return_value=[0.0] * 768))

    out = await ingest.ingest(client)

    assert out["new"] == 1
    assert out["chunks"] >= 1
    upsert.assert_awaited_once()
    rows = upsert.await_args.args[0]
    assert rows[0]["embedding"] == [0.0] * 768
    assert rows[0]["external_id"] == "https://blog.cs/karambit"


@pytest.mark.asyncio
async def test_ingest_skips_seen(monkeypatch):
    monkeypatch.setattr(ingest, "RAG_FEEDS", ["http://feed"])
    resp = MagicMock(text=RSS, status_code=200)
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    monkeypatch.setattr(ingest.repo, "seen_external_ids",
                        AsyncMock(return_value={"https://blog.cs/karambit"}))
    embed = AsyncMock(return_value=[0.0] * 768)
    monkeypatch.setattr(ingest.embeddings, "embed_text", embed)
    monkeypatch.setattr(ingest.repo, "upsert_chunks", AsyncMock())

    out = await ingest.ingest(client)

    assert out["new"] == 0
    embed.assert_not_awaited()
```

- [ ] **Step 3: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_rag_ingest.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'rag.ingest'`.

- [ ] **Step 4: Implementar `rag/ingest.py`**

```python
"""Ingesta del corpus RAG: fetch de feeds RSS → limpieza → chunking →
embedding de lo nuevo → upsert en Supabase.

Idempotente: dedup por external_id contra la tabla antes de embeddear, así
las corridas repetidas casi no gastan cuota de embeddings.
"""
import logging
from datetime import datetime, timezone

import feedparser
import httpx

from settings import RAG_FEEDS
from steam.mappers import _clean_news_content
from rag import embeddings, repo

logger = logging.getLogger("uvicorn.error")

_CHUNK_MAX_CHARS = 2000
# límite alto: no queremos que _clean_news_content trunque el cuerpo de la noticia.
_CLEAN_MAX = 100_000


def chunk_text(text: str, max_chars: int = _CHUNK_MAX_CHARS) -> list[str]:
    """Parte texto en trozos <= max_chars respetando límites de palabra.

    Texto corto → un solo trozo. Texto vacío/blanco → lista vacía.
    """
    text = " ".join(text.split())
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    words = text.split(" ")
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            chunks.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def _entry_published(entry) -> str | None:
    tm = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not tm:
        return None
    return datetime(*tm[:6], tzinfo=timezone.utc).isoformat()


def parse_feed_entries(feeds_xml: list[str]) -> list[dict]:
    """Parsea XML RSS crudo → items normalizados con texto limpio."""
    items: list[dict] = []
    for xml in feeds_xml:
        parsed = feedparser.parse(xml)
        source = (parsed.feed.get("title") or "rss")[:120]
        for e in parsed.entries:
            external_id = e.get("id") or e.get("link") or ""
            if not external_id:
                continue
            raw = e.get("summary") or e.get("description") or ""
            content = _clean_news_content(raw, max_chars=_CLEAN_MAX)
            if not content:
                continue
            items.append({
                "external_id": external_id,
                "title": e.get("title", ""),
                "url": e.get("link", ""),
                "content": content,
                "published_at": _entry_published(e),
                "source": source,
            })
    return items


async def _fetch_feeds(client: httpx.AsyncClient) -> list[str]:
    xmls: list[str] = []
    for url in RAG_FEEDS:
        try:
            resp = await client.get(url, timeout=20.0)
            resp.raise_for_status()
            xmls.append(resp.text)
        except httpx.HTTPError as exc:
            logger.warning("rag ingest: fallo al fetch %s: %s", url, exc)
    return xmls


async def ingest(client: httpx.AsyncClient) -> dict:
    """Corre una ingesta completa. Devuelve {fetched, new, chunks}."""
    xmls = await _fetch_feeds(client)
    items = parse_feed_entries(xmls)
    if not items:
        return {"fetched": 0, "new": 0, "chunks": 0}

    ids = [it["external_id"] for it in items]
    seen = await repo.seen_external_ids(ids)
    fresh = [it for it in items if it["external_id"] not in seen]

    rows: list[dict] = []
    for it in fresh:
        for idx, chunk in enumerate(chunk_text(it["content"])):
            vector = await embeddings.embed_text(client, chunk)
            rows.append({
                "source": it["source"],
                "external_id": it["external_id"],
                "chunk_index": idx,
                "title": it["title"],
                "url": it["url"],
                "content": chunk,
                "published_at": it["published_at"],
                "embedding": vector,
            })

    await repo.upsert_chunks(rows)
    return {"fetched": len(items), "new": len(fresh), "chunks": len(rows)}
```

- [ ] **Step 5: Correr el test para verlo pasar**

Run: `venv\Scripts\python -m pytest tests/test_rag_ingest.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Commit local (sin push)**

```bash
git add rag/ingest.py requirements.txt tests/test_rag_ingest.py
git commit -m "feat(rag): ingesta de feeds RSS (fetch, limpieza, chunking, dedup)"
```

---

## Task 4b: Fuente Steam News API (JSON) en `rag/ingest.py`

**Files:**
- Modify: `rag/ingest.py`
- Test: `tests/test_rag_ingest.py` (añadir casos)

**Interfaces:**
- Consumes: Steam News API (`GetNewsForApp` appid 730, JSON), `steam.mappers._clean_news_content`; `httpx.AsyncClient`.
- Produces:
  - `def parse_steam_news(payload: dict) -> list[dict]` — items JSON de Steam News → mismos dicts que `parse_feed_entries` (`{external_id, title, url, content, published_at, source}`).
  - `ingest()` ahora fusiona items de RSS **y** Steam News antes de dedup/embed.

Nota: Steam News items tienen las claves `gid`, `title`, `url`, `contents`, `date` (unix segundos), `feedlabel`. `external_id = "steam:" + gid` para no colisionar con guids de RSS.

- [ ] **Step 1: Escribir el test que falla**

Añadir a `tests/test_rag_ingest.py`:

```python
STEAM_NEWS = {
    "appnews": {"newsitems": [
        {"gid": "555", "title": "Operación nueva", "url": "https://store.steam/op",
         "contents": "Valve anuncia una nueva operación con cajas.",
         "date": 1752566400, "feedlabel": "Steam Community"},
    ]}
}


def test_parse_steam_news_extracts_fields():
    items = ingest.parse_steam_news(STEAM_NEWS)
    assert len(items) == 1
    it = items[0]
    assert it["external_id"] == "steam:555"
    assert it["title"] == "Operación nueva"
    assert "operación" in it["content"].lower()
    assert it["published_at"] is not None


def test_parse_steam_news_empty_payload():
    assert ingest.parse_steam_news({}) == []
```

Y actualizar `test_ingest_embeds_only_new` para que el mock del cliente devuelva
también el JSON de Steam News: cambiar `client.get = AsyncMock(return_value=resp)`
por un `side_effect` que devuelva el RSS para las URLs de feed y el JSON para la
URL de Steam News:

```python
    news_resp = MagicMock(status_code=200)
    news_resp.raise_for_status = MagicMock()
    news_resp.json = MagicMock(return_value=STEAM_NEWS)

    async def _get(url, **kw):
        return news_resp if "GetNewsForApp" in url else resp
    client.get = AsyncMock(side_effect=_get)
```

Con eso, `out["new"]` pasa a ser `2` (un item RSS + un item Steam News). Ajustar
esa aserción a `assert out["new"] == 2`.

- [ ] **Step 2: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_rag_ingest.py -v`
Expected: FAIL (`AttributeError: ... has no attribute 'parse_steam_news'` y el conteo `new`).

- [ ] **Step 3: Implementar en `rag/ingest.py`**

Añadir la constante de URL cerca de los imports:

```python
_STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
_STEAM_NEWS_COUNT = 20
```

Añadir la función de parseo (junto a `parse_feed_entries`):

```python
def parse_steam_news(payload: dict) -> list[dict]:
    """Items de la Steam News API (JSON) → mismos dicts que parse_feed_entries."""
    items: list[dict] = []
    newsitems = (payload.get("appnews") or {}).get("newsitems") or []
    for n in newsitems:
        gid = n.get("gid")
        if not gid:
            continue
        content = _clean_news_content(n.get("contents", ""), max_chars=_CLEAN_MAX)
        if not content:
            continue
        ts = n.get("date")
        published = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
        )
        items.append({
            "external_id": f"steam:{gid}",
            "title": n.get("title", ""),
            "url": n.get("url", ""),
            "content": content,
            "published_at": published,
            "source": n.get("feedlabel") or "steam_news",
        })
    return items
```

Añadir el fetch de Steam News y fusionarlo en `ingest()`. Reemplazar el inicio de
`ingest()` (las líneas `xmls = ...` / `items = parse_feed_entries(xmls)`) por:

```python
    xmls = await _fetch_feeds(client)
    items = parse_feed_entries(xmls)
    items += await _fetch_steam_news(client)
```

Y añadir el helper:

```python
async def _fetch_steam_news(client: httpx.AsyncClient) -> list[dict]:
    try:
        resp = await client.get(
            _STEAM_NEWS_URL,
            params={"appid": 730, "count": _STEAM_NEWS_COUNT, "format": "json"},
            timeout=20.0,
        )
        resp.raise_for_status()
        return parse_steam_news(resp.json())
    except httpx.HTTPError as exc:
        logger.warning("rag ingest: fallo al fetch Steam News: %s", exc)
        return []
```

- [ ] **Step 4: Correr los tests para verlos pasar**

Run: `venv\Scripts\python -m pytest tests/test_rag_ingest.py -v`
Expected: PASS (todos, incluidos los 2 nuevos).

- [ ] **Step 5: Commit local (sin push)**

```bash
git add rag/ingest.py tests/test_rag_ingest.py
git commit -m "feat(rag): fuente Steam News API (JSON) en la ingesta"
```

---

## Task 5: Recuperación (`rag/retrieval.py`)

**Files:**
- Create: `rag/retrieval.py`
- Test: `tests/test_rag_retrieval.py`

**Interfaces:**
- Consumes: `rag.embeddings.embed_text`, `rag.repo.match_chunks`; `httpx.AsyncClient`.
- Produces: `async def retrieve(client: httpx.AsyncClient, query: str, k: int = 5) -> list[dict]` — embeddea la query y devuelve los chunks de `match_chunks`. Query vacía → `[]`. **Pieza reutilizable** (futura tool `buscar_contexto_rag` de fase 2).

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/test_rag_retrieval.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from rag import retrieval


@pytest.mark.asyncio
async def test_retrieve_embeds_and_matches(monkeypatch):
    monkeypatch.setattr(retrieval.embeddings, "embed_text",
                        AsyncMock(return_value=[0.5] * 768))
    match = AsyncMock(return_value=[{"content": "c", "title": "t",
                                     "url": "u", "similarity": 0.8}])
    monkeypatch.setattr(retrieval.repo, "match_chunks", match)

    out = await retrieval.retrieve(MagicMock(), "por que sube el karambit", k=3)

    assert out[0]["content"] == "c"
    match.assert_awaited_once_with([0.5] * 768, 3)


@pytest.mark.asyncio
async def test_retrieve_empty_query_returns_empty(monkeypatch):
    embed = AsyncMock()
    monkeypatch.setattr(retrieval.embeddings, "embed_text", embed)
    out = await retrieval.retrieve(MagicMock(), "   ")
    assert out == []
    embed.assert_not_awaited()
```

- [ ] **Step 2: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_rag_retrieval.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'rag.retrieval'`.

- [ ] **Step 3: Implementar `rag/retrieval.py`**

```python
"""Recuperación de contexto para el RAG: embeddea una consulta y busca los
chunks más similares en Supabase (pgvector).

Unidad reutilizable: la fase 2 (orquestador con function calling) la expondrá
como la tool `buscar_contexto_rag`.
"""
import httpx

from rag import embeddings, repo


async def retrieve(client: httpx.AsyncClient, query: str, k: int = 5) -> list[dict]:
    """Top-k chunks más similares a `query`. Query vacía → []."""
    query = (query or "").strip()
    if not query:
        return []
    vector = await embeddings.embed_text(client, query)
    return await repo.match_chunks(vector, k)
```

- [ ] **Step 4: Correr el test para verlo pasar**

Run: `venv\Scripts\python -m pytest tests/test_rag_retrieval.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit local (sin push)**

```bash
git add rag/retrieval.py tests/test_rag_retrieval.py
git commit -m "feat(rag): recuperación reutilizable (embed query + match)"
```

---

## Task 6: Generación con contexto (`rag/gemini.py`)

**Files:**
- Modify: `rag/gemini.py`
- Test: `tests/test_rag_gemini.py`

**Interfaces:**
- Consumes: `settings.GEMINI_API_KEY`, `settings.GEMINI_MODEL`; `httpx.AsyncClient`; chunks de `retrieve` (`{title, url, content, ...}`).
- Produces: `async def generate_with_context(client, question: str, chunks: list[dict]) -> str` — arma un prompt con el contexto y una instrucción de no inventar; devuelve el texto. Chunks vacío → sigue generando pero el prompt indica que no hay contexto (Gemini responderá que no tiene datos).

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/test_rag_gemini.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from rag import gemini


@pytest.mark.asyncio
async def test_generate_with_context_includes_chunks(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(gemini, "GEMINI_MODEL", "gemini-flash-latest")

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "candidates": [{"content": {"parts": [{"text": "Subió por la operación."}]}}]
    })
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)

    chunks = [{"title": "Op nueva", "url": "u", "content": "Nueva operación en CS2."}]
    out = await gemini.generate_with_context(client, "por que sube?", chunks)

    assert out == "Subió por la operación."
    body = client.post.call_args.kwargs["json"]
    sent = str(body)
    assert "Nueva operación en CS2." in sent  # el contexto viaja en el prompt


@pytest.mark.asyncio
async def test_generate_with_context_raises_without_key(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError):
        await gemini.generate_with_context(MagicMock(), "x", [])
```

- [ ] **Step 2: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_rag_gemini.py -v`
Expected: FAIL con `AttributeError: module 'rag.gemini' has no attribute 'generate_with_context'`.

- [ ] **Step 3: Añadir `generate_with_context` a `rag/gemini.py`**

Añadir al final del archivo (reutiliza `_GEMINI_BASE`, `_GEMINI_TIMEOUT` ya definidos arriba):

```python
_RAG_SYSTEM_PROMPT = (
    "Eres Sharky 🦈, asistente de CS-FINANCE experto en el mercado de skins de "
    "Counter-Strike 2. Respondes en español, claro y conciso. Usa ÚNICAMENTE la "
    "información del CONTEXTO para responder. Si el contexto no contiene la "
    "respuesta, dilo explícitamente en vez de inventar."
)


def _format_context(chunks: list[dict]) -> str:
    if not chunks:
        return "(No hay noticias relevantes en la base.)"
    blocks = []
    for c in chunks:
        title = c.get("title") or "(sin título)"
        url = c.get("url") or ""
        content = c.get("content") or ""
        blocks.append(f"### {title}\n{content}\nFuente: {url}")
    return "\n\n".join(blocks)


async def generate_with_context(
    client: httpx.AsyncClient,
    question: str,
    chunks: list[dict],
) -> str:
    """Genera una respuesta usando los chunks recuperados como contexto.

    Lanza RuntimeError si falta la key o la respuesta viene vacía/bloqueada;
    propaga los errores httpx.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY no configurada")

    prompt = (
        f"CONTEXTO:\n{_format_context(chunks)}\n\n"
        f"PREGUNTA DEL USUARIO:\n{question}"
    )
    body = {
        "systemInstruction": {"parts": [{"text": _RAG_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    url = f"{_GEMINI_BASE}/{GEMINI_MODEL}:generateContent"
    resp = await client.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json=body,
        timeout=_GEMINI_TIMEOUT,
    )
    resp.raise_for_status()

    candidates = resp.json().get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini no devolvió respuesta (posible bloqueo de seguridad)")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("Respuesta vacía de Gemini")
    return text
```

- [ ] **Step 4: Correr el test para verlo pasar**

Run: `venv\Scripts\python -m pytest tests/test_rag_gemini.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit local (sin push)**

```bash
git add rag/gemini.py tests/test_rag_gemini.py
git commit -m "feat(rag): generación con contexto recuperado (no inventar)"
```

---

## Task 7: Endpoints (`rag/router.py`)

**Files:**
- Modify: `rag/router.py`
- Test: `tests/test_rag_router.py`

**Interfaces:**
- Consumes: `rag.retrieval.retrieve`, `rag.gemini.generate_with_context`, `rag.ingest.ingest`, `settings.RAG_INGEST_TOKEN`; `auth.service.require_jwt/_rate_limit/_get_client_ip`.
- Produces:
  - `POST /rag/ask` (Bearer JWT) → `{"reply": str, "sources": [{"title","url","published_at"}]}`.
  - `POST /internal/rag-ingest` (header `X-Rag-Ingest-Token`, `secrets.compare_digest`) → resultado de `ingest`.

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/test_rag_router.py`:

```python
from unittest.mock import AsyncMock

from rag import router as rag_router


def test_ask_returns_reply_and_sources(client, monkeypatch):
    monkeypatch.setattr(rag_router, "retrieve", AsyncMock(return_value=[
        {"title": "Op nueva", "url": "https://u", "published_at": "2026-07-15T00:00:00Z",
         "content": "Nueva operación."}
    ]))
    monkeypatch.setattr(rag_router, "generate_with_context",
                        AsyncMock(return_value="Subió por la operación."))

    resp = client.post("/rag/ask", json={"question": "por que sube el karambit?"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["reply"] == "Subió por la operación."
    assert data["sources"] == [
        {"title": "Op nueva", "url": "https://u", "published_at": "2026-07-15T00:00:00Z"}
    ]


def test_ask_rejects_empty_question(client):
    resp = client.post("/rag/ask", json={"question": "   "})
    assert resp.status_code == 400


def test_rag_ingest_requires_token(client, monkeypatch):
    monkeypatch.setattr(rag_router, "RAG_INGEST_TOKEN", "secret123")
    resp = client.post("/internal/rag-ingest")
    assert resp.status_code == 401


def test_rag_ingest_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr(rag_router, "RAG_INGEST_TOKEN", "secret123")
    resp = client.post("/internal/rag-ingest", headers={"X-Rag-Ingest-Token": "wrong"})
    assert resp.status_code == 401


def test_rag_ingest_runs_with_valid_token(client, monkeypatch):
    monkeypatch.setattr(rag_router, "RAG_INGEST_TOKEN", "secret123")
    monkeypatch.setattr(rag_router, "ingest",
                        AsyncMock(return_value={"fetched": 3, "new": 1, "chunks": 2}))
    resp = client.post("/internal/rag-ingest", headers={"X-Rag-Ingest-Token": "secret123"})
    assert resp.status_code == 200
    assert resp.json() == {"fetched": 3, "new": 1, "chunks": 2}
```

- [ ] **Step 2: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_rag_router.py -v`
Expected: FAIL (404 en `/rag/ask` y `/internal/rag-ingest`; o AttributeError por los símbolos aún no importados).

- [ ] **Step 3: Añadir los endpoints a `rag/router.py`**

Añadir imports arriba (junto a los existentes):

```python
import secrets
from fastapi import Header

from settings import RAG_INGEST_TOKEN
from .retrieval import retrieve
from .gemini import generate_reply, generate_with_context
from .ingest import ingest
```

(Nota: la línea existente `from .gemini import generate_reply` se reemplaza por la de arriba que trae ambos.)

Añadir los modelos y rutas al final del archivo:

```python
class AskRequest(BaseModel):
    question: str


class Source(BaseModel):
    title: str = ""
    url: str = ""
    published_at: str | None = None


class AskResponse(BaseModel):
    reply: str
    sources: list[Source] = []


@router.post("/rag/ask", response_model=AskResponse, summary="Pregunta al RAG de noticias CS2")
async def rag_ask(
    payload: AskRequest,
    request: Request,
    _claims: dict = Depends(require_jwt),
):
    _rate_limit(_get_client_ip(request))

    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="La pregunta está vacía")

    client = request.app.state.http_client
    try:
        chunks = await retrieve(client, question)
        reply = await generate_with_context(client, question, chunks)
    except httpx.HTTPStatusError as exc:
        logger.warning("rag_ask Gemini %s: %s", exc.response.status_code, exc.response.text[:300])
        raise HTTPException(status_code=502, detail="El asistente no está disponible ahora mismo")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar con el asistente: {exc}")
    except RuntimeError as exc:
        logger.warning("rag_ask: %s", exc)
        raise HTTPException(status_code=503, detail="El asistente no está configurado")

    sources = [
        Source(title=c.get("title", ""), url=c.get("url", ""),
               published_at=c.get("published_at"))
        for c in chunks
    ]
    return AskResponse(reply=reply, sources=sources)


@router.post("/internal/rag-ingest", summary="Ingesta de noticias del RAG (cron)")
async def rag_ingest(
    request: Request,
    x_rag_ingest_token: str = Header(default=""),
):
    if not RAG_INGEST_TOKEN or not secrets.compare_digest(x_rag_ingest_token, RAG_INGEST_TOKEN):
        raise HTTPException(status_code=401, detail="Token inválido")
    return await ingest(request.app.state.http_client)
```

- [ ] **Step 4: Correr el test para verlo pasar**

Run: `venv\Scripts\python -m pytest tests/test_rag_router.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Correr toda la suite RAG + regresión**

Run: `venv\Scripts\python -m pytest tests/ -v`
Expected: PASS — todos los tests nuevos verdes y ningún test previo roto.

- [ ] **Step 6: Commit local (sin push)**

```bash
git add rag/router.py tests/test_rag_router.py
git commit -m "feat(rag): endpoints POST /rag/ask y POST /internal/rag-ingest"
```

---

## Task 8: Cron de ingesta (GitHub Actions)

**Files:**
- Create: `.github/workflows/rag-ingest.yml`

**Interfaces:**
- Consumes: secrets de GitHub Actions `RENDER_API_BASE` (o URL fija del backend) y `RAG_INGEST_TOKEN`.

- [ ] **Step 1: Revisar el workflow existente como plantilla**

Run: `cat .github/workflows/cap-tick.yml`
Expected: ver el patrón (schedule cron + `curl -X POST` con header de token a la URL de Render).

- [ ] **Step 2: Crear `.github/workflows/rag-ingest.yml`**

Espejar el patrón de `cap-tick.yml`, ajustando ruta, header y frecuencia (diaria). Base:

```yaml
name: rag-ingest
on:
  schedule:
    - cron: "20 6 * * *"   # diario 06:20 UTC
  workflow_dispatch:
jobs:
  ingest:
    runs-on: ubuntu-latest
    steps:
      - name: POST /internal/rag-ingest
        run: |
          curl -fsS -X POST "https://cs-finance-api.onrender.com/internal/rag-ingest" \
            -H "X-Rag-Ingest-Token: ${{ secrets.RAG_INGEST_TOKEN }}" \
            --max-time 120
```

(Si `cap-tick.yml` usa una variable de URL/secret distinta, replicar ese estilo exacto en lugar de la URL fija.)

- [ ] **Step 3: Validar el YAML**

Run: `venv\Scripts\python -c "import yaml; yaml.safe_load(open('.github/workflows/rag-ingest.yml')); print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit local (sin push)**

```bash
git add .github/workflows/rag-ingest.yml
git commit -m "feat(rag): workflow GitHub Actions rag-ingest (cron diario)"
```

---

## Cierre (manual, tras aprobar el operador)

Estos pasos NO se ejecutan en esta sesión (sin commit/merge), quedan para el operador:

1. Correr `docs/sql/rag_chunks.sql` en el SQL editor del proyecto Supabase `cs-finance`.
2. Configurar en `.env` local, secrets de Render y secrets de GitHub Actions: `RAG_INGEST_TOKEN` (generar con `secrets.token_urlsafe(32)`), y opcionalmente `RAG_FEEDS` / `GEMINI_EMBED_MODEL`.
3. Disparar el workflow `rag-ingest` a mano (`workflow_dispatch`) para la primera carga del corpus.
4. Probar `POST /rag/ask` con una pregunta real y verificar `sources`.

---

## Self-Review (cobertura del spec)

- Extensión pgvector + tabla `rag_chunks` + índice hnsw + RPC → Task 1 ✅
- Cliente embeddings Gemini 768 → Task 2 ✅
- Capa Supabase (upsert/match/dedup) → Task 3 ✅
- Ingesta RSS + limpieza + chunking + dedup → Task 4 ✅
- Ingesta Steam News API (JSON) → Task 4b ✅
- Retrieval reutilizable → Task 5 ✅
- Generación con contexto + no-inventar → Task 6 ✅
- `POST /rag/ask` con `sources` + `POST /internal/rag-ingest` con token → Task 7 ✅
- Cron GitHub Actions → Task 8 ✅
- Variables de entorno + startup warning → Task 1 ✅

**Fuentes:** RSS (Task 4, `feedparser`) + Steam News API JSON (Task 4b, `parse_steam_news`). Ambas producen el mismo dict normalizado y comparten dedup/chunking/embedding en `ingest()`.
```