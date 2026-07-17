# Captura de precios históricos por-skin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Empezar a acumular una serie temporal de precios por-skin en Supabase (base de datos para el futuro modelo de predicción), vía un cron diario que snapshotea un conjunto de skins seguidas (seed curado + auto-registro de inventarios).

**Architecture:** Dos tablas Supabase (`tracked_skins`, `precios_historicos`). Un seed curado (`steam/data/tracked_seed.json`) siembra las populares; `/inventory` auto-registra las skins vistas. Un cron de GitHub Actions diario → `POST /internal/price-tick` recorre las skins seguidas (priorizando la menos-recientemente-capturada, con tope por corrida), hace lookup por-nombre `GET /item?market_hash_name=`, y hace upsert de `(name, date, price, volume)`. Sigue el patrón cron→endpoint-interno-con-token y la capa Supabase sync→`asyncio.to_thread` ya existentes.

**Tech Stack:** Python 3 / FastAPI, Supabase (Postgres), steamwebapi (`/item`), `httpx.AsyncClient` compartido, `_SlidingWindowLimiter` existente, pytest + pytest-asyncio.

## Global Constraints

- **Commits locales SÍ; push y merge NO** (instrucción del operador). Cada tarea termina en commit local en `feat/rag-chat`. Mensaje `feat(price): ...`, cuerpo terminado en `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Nunca `git push`/`git merge`.
- Stack 100% free: Supabase free, GitHub Actions, steamwebapi Starter (**20 req/60s por endpoint, 2.000/día** — respetar con el `_SlidingWindowLimiter` existente + `PRICE_LOOKUP_CAP`). Sin servicios de pago.
- Tests: `venv\Scripts\python -m pytest` (el Python del sistema no tiene firebase_admin). `pytest-asyncio` con `asyncio_mode=auto` ya está configurado. Correr la suite completa antes de cada commit.
- Capa Supabase: cliente cacheado module-level con `service_role`, llamadas sync en `asyncio.to_thread` (patrón `steam/cap_history_repo.py`). RLS habilitado sin policies.
- Archivos nuevos en UTF-8. No tocar la codificación de `requirements.txt` (UTF-8).
- Precio canónico = `pricelatestsell` (con fallback `pricelatest`/`pricemedian`), volumen = `sold24h` — misma prioridad que `_map_item`.
- Números exactos por consulta directa, nunca por RAG/embeddings.

---

## File Structure

- `docs/sql/precios_historicos.sql` — **crear**. SQL a correr a mano en Supabase.
- `settings.py` — **modificar**. `PRICE_TICK_TOKEN`, `PRICE_LOOKUP_CAP`.
- `main.py` — **modificar**. Warning de startup si falta `PRICE_TICK_TOKEN`; seed idempotente en el lifespan.
- `steam/price_history_repo.py` — **crear**. Capa Supabase (`tracked_skins` + `precios_historicos`).
- `steam/data/tracked_seed.json` — **crear**. Seed curado de nombres.
- `steam/price_capture.py` — **crear**. `seed_tracked` + `capture` + extracción de precio.
- `steam/routes/market.py` — **modificar**. `POST /internal/price-tick`.
- `steam/routes/items.py` — **modificar**. Auto-registro en `/inventory` (best-effort).
- `.github/workflows/price-tick.yml` — **crear**. Cron diario.
- `.env.example` — **modificar**. Documentar las variables nuevas.
- `tests/test_price_repo.py`, `tests/test_price_capture.py`, `tests/test_price_tick_router.py` — **crear**.

---

## Task 1: Esquema SQL + configuración

**Files:**
- Create: `docs/sql/precios_historicos.sql`
- Modify: `settings.py` (tras el bloque RAG, al final)
- Modify: `main.py` (import de settings + warning en el lifespan)
- Modify: `.env.example`

**Interfaces:**
- Produces: `settings.PRICE_TICK_TOKEN: str`, `settings.PRICE_LOOKUP_CAP: int`. Tablas `public.tracked_skins` y `public.precios_historicos`.

- [ ] **Step 1: Escribir el SQL**

Crear `docs/sql/precios_historicos.sql`:

```sql
-- Captura de precios históricos por-skin — correr en el SQL editor del proyecto
-- Supabase `cs-finance` (el mismo de market_cap_history).

create table if not exists public.tracked_skins (
    market_hash_name text primary key,
    source           text not null,            -- 'top_n' | 'inventory'
    first_seen       timestamptz not null default now(),
    last_captured    date                        -- null = nunca capturada (prioridad máxima)
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

- [ ] **Step 2: Variables en `settings.py`**

Al final de `settings.py` (tras el bloque RAG):

```python
# Captura de precios históricos por-skin (POST /internal/price-tick, cron diario).
PRICE_TICK_TOKEN = os.getenv("PRICE_TICK_TOKEN", "")
PRICE_LOOKUP_CAP = int(os.getenv("PRICE_LOOKUP_CAP", "400"))
```

- [ ] **Step 3: Warning de startup en `main.py`**

Añadir `PRICE_TICK_TOKEN` a la tupla `from settings import (...)`. Tras el bloque `if not RAG_INGEST_TOKEN:` añadir:

```python
    if not PRICE_TICK_TOKEN:
        logger.warning(
            "PRICE_TICK_TOKEN no está configurada — "
            "la captura de precios históricos (POST /internal/price-tick) no funcionará"
        )
```

- [ ] **Step 4: Documentar en `.env.example`**

Añadir cerca del bloque RAG:

```
# Captura de precios históricos por-skin
PRICE_TICK_TOKEN=
PRICE_LOOKUP_CAP=400
```

- [ ] **Step 5: Verificar import**

Run: `venv\Scripts\python -c "import settings, main; print(settings.PRICE_TICK_TOKEN == '', settings.PRICE_LOOKUP_CAP)"`
Expected: `True 400`

- [ ] **Step 6: Commit local (sin push)**

```bash
git add docs/sql/precios_historicos.sql settings.py main.py .env.example
git commit -m "feat(price): esquema SQL (tracked_skins, precios_historicos) + config"
```

---

## Task 2: Capa Supabase (`steam/price_history_repo.py`)

**Files:**
- Create: `steam/price_history_repo.py`
- Test: `tests/test_price_repo.py`

**Interfaces:**
- Consumes: `settings.SUPABASE_URL`, `settings.SUPABASE_SERVICE_KEY`; `supabase.create_client`.
- Produces:
  - `async def register_tracked(names: list[str], source: str) -> None` — upsert (no-op si vacío); no pisa filas existentes (usa `on_conflict` + `ignore_duplicates`).
  - `async def fetch_tracked(limit: int) -> list[str]` — hasta `limit` nombres ordenados por `last_captured` asc, nulls primero.
  - `async def upsert_prices(rows: list[dict]) -> None` — upsert por `(market_hash_name, date)`; no-op si vacío.
  - `async def mark_captured(names: list[str], date_iso: str) -> None` — set `last_captured` para esos nombres; no-op si vacío.
  - `async def count_tracked() -> int` — nº de filas en `tracked_skins`.
  - `def get_supabase() -> Client`.

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/test_price_repo.py`:

```python
import pytest
from unittest.mock import MagicMock

from steam import price_history_repo as repo


@pytest.mark.asyncio
async def test_register_tracked_noop_on_empty(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    await repo.register_tracked([], "top_n")
    client.table.assert_not_called()


@pytest.mark.asyncio
async def test_register_tracked_upserts_rows(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    await repo.register_tracked(["AK-47 | Redline (Field-Tested)"], "inventory")
    args, kwargs = client.table.return_value.upsert.call_args
    rows = args[0]
    assert rows[0]["market_hash_name"] == "AK-47 | Redline (Field-Tested)"
    assert rows[0]["source"] == "inventory"
    assert kwargs.get("ignore_duplicates") is True
    assert kwargs.get("on_conflict") == "market_hash_name"


@pytest.mark.asyncio
async def test_fetch_tracked_orders_nulls_first(monkeypatch):
    client = MagicMock()
    chain = client.table.return_value.select.return_value.order.return_value.limit.return_value
    chain.execute.return_value = MagicMock(data=[{"market_hash_name": "a"}, {"market_hash_name": "b"}])
    monkeypatch.setattr(repo, "get_supabase", lambda: client)

    out = await repo.fetch_tracked(50)

    assert out == ["a", "b"]
    client.table.return_value.select.return_value.order.assert_called_once_with(
        "last_captured", desc=False, nullsfirst=True
    )
    client.table.return_value.select.return_value.order.return_value.limit.assert_called_once_with(50)


@pytest.mark.asyncio
async def test_upsert_prices_noop_on_empty(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    await repo.upsert_prices([])
    client.table.assert_not_called()


@pytest.mark.asyncio
async def test_mark_captured_sets_date(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    await repo.mark_captured(["a", "b"], "2026-07-17")
    client.table.return_value.update.assert_called_once_with({"last_captured": "2026-07-17"})
    client.table.return_value.update.return_value.in_.assert_called_once_with(
        "market_hash_name", ["a", "b"]
    )


@pytest.mark.asyncio
async def test_count_tracked(monkeypatch):
    client = MagicMock()
    client.table.return_value.select.return_value.execute.return_value = MagicMock(count=7)
    monkeypatch.setattr(repo, "get_supabase", lambda: client)
    assert await repo.count_tracked() == 7
```

- [ ] **Step 2: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_price_repo.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'steam.price_history_repo'`.

- [ ] **Step 3: Implementar `steam/price_history_repo.py`**

```python
"""Capa Supabase para la captura de precios históricos por-skin.

Dos tablas: tracked_skins (qué seguimos) y precios_historicos (la serie).
supabase-py es síncrono → todas las llamadas se envuelven en asyncio.to_thread.
Cliente cacheado module-level con service_role (bypassa RLS), patrón
steam/cap_history_repo.py.
"""
import asyncio

from supabase import create_client, Client

from settings import SUPABASE_URL, SUPABASE_SERVICE_KEY

_TRACKED = "tracked_skins"
_PRICES = "precios_historicos"
_client: Client | None = None


def get_supabase() -> Client:
    global _client
    if _client is None:
        if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_SERVICE_KEY no configuradas — "
                "no se puede acceder a la captura de precios"
            )
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


async def register_tracked(names: list[str], source: str) -> None:
    """Registra nombres en tracked_skins. No-op si vacío. No pisa filas existentes."""
    if not names:
        return
    rows = [{"market_hash_name": n, "source": source} for n in dict.fromkeys(names)]

    def _do() -> None:
        (get_supabase().table(_TRACKED)
            .upsert(rows, on_conflict="market_hash_name", ignore_duplicates=True)
            .execute())

    await asyncio.to_thread(_do)


async def fetch_tracked(limit: int) -> list[str]:
    """Hasta `limit` nombres, menos-recientemente-capturados primero (nulls primero)."""
    def _do() -> list[str]:
        resp = (get_supabase().table(_TRACKED)
                .select("market_hash_name")
                .order("last_captured", desc=False, nullsfirst=True)
                .limit(limit)
                .execute())
        return [r["market_hash_name"] for r in (resp.data or [])]

    return await asyncio.to_thread(_do)


async def upsert_prices(rows: list[dict]) -> None:
    """Upsert de snapshots por (market_hash_name, date). No-op si vacío."""
    if not rows:
        return

    def _do() -> None:
        (get_supabase().table(_PRICES)
            .upsert(rows, on_conflict="market_hash_name,date")
            .execute())

    await asyncio.to_thread(_do)


async def mark_captured(names: list[str], date_iso: str) -> None:
    """Marca last_captured=date_iso para los nombres dados. No-op si vacío."""
    if not names:
        return

    def _do() -> None:
        (get_supabase().table(_TRACKED)
            .update({"last_captured": date_iso})
            .in_("market_hash_name", names)
            .execute())

    await asyncio.to_thread(_do)


async def count_tracked() -> int:
    """Número de filas en tracked_skins (para el seed idempotente)."""
    def _do() -> int:
        resp = (get_supabase().table(_TRACKED)
                .select("market_hash_name", count="exact")
                .execute())
        return resp.count or 0

    return await asyncio.to_thread(_do)
```

- [ ] **Step 4: Correr el test para verlo pasar**

Run: `venv\Scripts\python -m pytest tests/test_price_repo.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit local (sin push)**

```bash
git add steam/price_history_repo.py tests/test_price_repo.py
git commit -m "feat(price): capa Supabase (tracked_skins + precios_historicos)"
```

---

## Task 3: Seed curado + captura (`steam/price_capture.py`)

**Files:**
- Create: `steam/data/tracked_seed.json`
- Create: `steam/price_capture.py`
- Test: `tests/test_price_capture.py`

**Interfaces:**
- Consumes: `steam.price_history_repo` (register/fetch/upsert/mark/count), `steam.services` (`STEAM_WEB_API`, `_history_limiter`), `settings` (`STEAM_API_KEY`, `PRICE_LOOKUP_CAP`); `httpx.AsyncClient`.
- Produces:
  - `def _canonical_price(item: dict) -> float | None` — `pricelatestsell` → `pricelatest` → `pricemedian`, primero > 0; si ninguno, `None`.
  - `async def seed_tracked() -> int` — si `count_tracked()==0`, registra el JSON con source `'top_n'`; devuelve nº registrado (0 si ya había).
  - `async def capture(client) -> dict` — snapshotea hasta `PRICE_LOOKUP_CAP` skins; devuelve `{"tracked_run": N, "captured": M, "skipped": S, "errors": E}`.

- [ ] **Step 1: Crear el seed `steam/data/tracked_seed.json`**

Lista curada de skins líquidas (market_hash_name exactos). Nombres verificados en el Step 4:

```json
[
  "AK-47 | Redline (Field-Tested)",
  "AK-47 | Redline (Minimal Wear)",
  "AK-47 | Asiimov (Field-Tested)",
  "AK-47 | Vulcan (Field-Tested)",
  "AK-47 | Neon Rider (Field-Tested)",
  "AK-47 | Bloodsport (Field-Tested)",
  "AK-47 | Phantom Disruptor (Field-Tested)",
  "AK-47 | Slate (Field-Tested)",
  "AK-47 | Point Disarray (Field-Tested)",
  "AWP | Asiimov (Field-Tested)",
  "AWP | Hyper Beast (Field-Tested)",
  "AWP | Neo-Noir (Field-Tested)",
  "AWP | Wildfire (Field-Tested)",
  "AWP | Chromatic Aberration (Field-Tested)",
  "AWP | Containment Breach (Field-Tested)",
  "AWP | Atheris (Field-Tested)",
  "AWP | Fever Dream (Field-Tested)",
  "M4A4 | Asiimov (Field-Tested)",
  "M4A4 | Desolate Space (Field-Tested)",
  "M4A4 | The Emperor (Field-Tested)",
  "M4A4 | Neo-Noir (Field-Tested)",
  "M4A1-S | Hyper Beast (Field-Tested)",
  "M4A1-S | Printstream (Field-Tested)",
  "M4A1-S | Golden Coil (Field-Tested)",
  "M4A1-S | Cyrex (Field-Tested)",
  "M4A1-S | Player Two (Field-Tested)",
  "Desert Eagle | Blaze (Factory New)",
  "Desert Eagle | Code Red (Field-Tested)",
  "Desert Eagle | Printstream (Field-Tested)",
  "Desert Eagle | Ocean Drive (Factory New)",
  "USP-S | Kill Confirmed (Field-Tested)",
  "USP-S | Neo-Noir (Field-Tested)",
  "USP-S | The Traitor (Field-Tested)",
  "USP-S | Cortex (Field-Tested)",
  "Glock-18 | Water Elemental (Field-Tested)",
  "Glock-18 | Fade (Factory New)",
  "Glock-18 | Gamma Doppler (Factory New)",
  "Glock-18 | Neo-Noir (Field-Tested)",
  "Desert Eagle | Mecha Industries (Factory New)",
  "AK-47 | The Empress (Field-Tested)",
  "AWP | Man-o'-war (Field-Tested)",
  "M4A4 | Howl (Field-Tested)",
  "AWP | Dragon Lore (Field-Tested)",
  "USP-S | Orion (Factory New)",
  "P250 | See Ya Later (Factory New)",
  "SSG 08 | Blood in the Water (Field-Tested)",
  "Five-SeveN | Hyper Beast (Field-Tested)",
  "Tec-9 | Fuel Injector (Field-Tested)",
  "Galil AR | Chatterbox (Field-Tested)",
  "FAMAS | Roll Cage (Field-Tested)"
]
```

- [ ] **Step 2: Escribir el test que falla**

Crear `tests/test_price_capture.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from steam import price_capture


def test_canonical_price_prefers_latestsell():
    assert price_capture._canonical_price(
        {"pricelatestsell": 43.15, "pricelatest": 41.35, "pricemedian": 42.71}
    ) == 43.15


def test_canonical_price_falls_back_when_zero():
    assert price_capture._canonical_price(
        {"pricelatestsell": 0, "pricelatest": 0, "pricemedian": 42.71}
    ) == 42.71


def test_canonical_price_none_when_all_missing():
    assert price_capture._canonical_price({}) is None


@pytest.mark.asyncio
async def test_seed_tracked_only_when_empty(monkeypatch):
    monkeypatch.setattr(price_capture.repo, "count_tracked", AsyncMock(return_value=0))
    reg = AsyncMock()
    monkeypatch.setattr(price_capture.repo, "register_tracked", reg)

    n = await price_capture.seed_tracked()

    assert n > 0
    reg.assert_awaited_once()
    assert reg.await_args.args[1] == "top_n"


@pytest.mark.asyncio
async def test_seed_tracked_skips_when_populated(monkeypatch):
    monkeypatch.setattr(price_capture.repo, "count_tracked", AsyncMock(return_value=5))
    reg = AsyncMock()
    monkeypatch.setattr(price_capture.repo, "register_tracked", reg)

    n = await price_capture.seed_tracked()

    assert n == 0
    reg.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_snapshots_and_marks(monkeypatch):
    monkeypatch.setattr(price_capture, "PRICE_LOOKUP_CAP", 400)
    monkeypatch.setattr(price_capture.repo, "fetch_tracked",
                        AsyncMock(return_value=["AK-47 | Redline (Field-Tested)"]))
    upsert = AsyncMock()
    mark = AsyncMock()
    monkeypatch.setattr(price_capture.repo, "upsert_prices", upsert)
    monkeypatch.setattr(price_capture.repo, "mark_captured", mark)
    # el lookup por-nombre devuelve un item con precio y volumen
    monkeypatch.setattr(price_capture, "_lookup_item",
                        AsyncMock(return_value={"pricelatestsell": 43.15, "sold24h": 69}))

    out = await price_capture.capture(MagicMock())

    assert out["captured"] == 1
    row = upsert.await_args.args[0][0]
    assert row["market_hash_name"] == "AK-47 | Redline (Field-Tested)"
    assert row["price"] == 43.15
    assert row["volume"] == 69
    mark.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_skips_item_without_price(monkeypatch):
    monkeypatch.setattr(price_capture, "PRICE_LOOKUP_CAP", 400)
    monkeypatch.setattr(price_capture.repo, "fetch_tracked",
                        AsyncMock(return_value=["Bad | Skin (Field-Tested)"]))
    upsert = AsyncMock()
    monkeypatch.setattr(price_capture.repo, "upsert_prices", upsert)
    monkeypatch.setattr(price_capture.repo, "mark_captured", AsyncMock())
    monkeypatch.setattr(price_capture, "_lookup_item",
                        AsyncMock(return_value={"pricelatestsell": 0}))

    out = await price_capture.capture(MagicMock())

    assert out["captured"] == 0
    assert out["skipped"] == 1
    upsert.assert_awaited_once()
    assert upsert.await_args.args[0] == []  # nada que upsertear


@pytest.mark.asyncio
async def test_capture_counts_errors(monkeypatch):
    monkeypatch.setattr(price_capture, "PRICE_LOOKUP_CAP", 400)
    monkeypatch.setattr(price_capture.repo, "fetch_tracked",
                        AsyncMock(return_value=["X | Y (Field-Tested)"]))
    monkeypatch.setattr(price_capture.repo, "upsert_prices", AsyncMock())
    monkeypatch.setattr(price_capture.repo, "mark_captured", AsyncMock())
    monkeypatch.setattr(price_capture, "_lookup_item",
                        AsyncMock(side_effect=RuntimeError("boom")))

    out = await price_capture.capture(MagicMock())

    assert out["errors"] == 1
    assert out["captured"] == 0
```

- [ ] **Step 3: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_price_capture.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'steam.price_capture'`.

- [ ] **Step 4: Implementar `steam/price_capture.py`**

```python
"""Seed + captura diaria de precios por-skin.

seed_tracked(): siembra tracked_skins desde el JSON curado si está vacía.
capture(): recorre las skins seguidas (menos-recientemente-capturadas primero,
hasta PRICE_LOOKUP_CAP), hace lookup por-nombre en steamwebapi /item vía el
limiter compartido, y hace upsert del snapshot del día. Best-effort: un fallo
por skin no aborta la corrida.
"""
import json
import logging
from datetime import date
from pathlib import Path

import httpx

from settings import STEAM_API_KEY, PRICE_LOOKUP_CAP
from steam.services import STEAM_WEB_API, _history_limiter
from steam import price_history_repo as repo

logger = logging.getLogger("uvicorn.error")

_SEED_PATH = Path(__file__).parent / "data" / "tracked_seed.json"
_LOOKUP_TIMEOUT = 20.0


def _canonical_price(item: dict) -> float | None:
    """Precio canónico: pricelatestsell → pricelatest → pricemedian (primero > 0)."""
    for key in ("pricelatestsell", "pricelatest", "pricemedian"):
        try:
            v = float(item.get(key) or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            return v
    return None


def _load_seed() -> list[str]:
    return json.loads(_SEED_PATH.read_text(encoding="utf-8"))


async def seed_tracked() -> int:
    """Registra el seed curado si tracked_skins está vacía. Devuelve nº registrado."""
    if await repo.count_tracked() > 0:
        return 0
    names = _load_seed()
    await repo.register_tracked(names, "top_n")
    logger.info("[price] seed: registradas %d skins", len(names))
    return len(names)


async def _lookup_item(client: httpx.AsyncClient, name: str) -> dict:
    """GET /item?market_hash_name=<name> vía el limiter compartido. Devuelve el item."""
    await _history_limiter.acquire()
    resp = await client.get(
        f"{STEAM_WEB_API}/item",
        params={"key": STEAM_API_KEY, "game": "cs2",
                "market_hash_name": name, "format": "json"},
        timeout=_LOOKUP_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()
    data = resp.json()
    return data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})


async def capture(client: httpx.AsyncClient) -> dict:
    """Snapshotea hasta PRICE_LOOKUP_CAP skins seguidas. Best-effort por skin."""
    names = await repo.fetch_tracked(PRICE_LOOKUP_CAP)
    today = date.today().isoformat()

    rows: list[dict] = []
    captured_names: list[str] = []
    skipped = 0
    errors = 0

    for name in names:
        try:
            item = await _lookup_item(client, name)
        except (httpx.HTTPError, ValueError) as exc:
            errors += 1
            logger.warning("[price] lookup falló para %r: %s", name, exc)
            continue

        price = _canonical_price(item)
        if price is None:
            skipped += 1
            continue

        volume = item.get("sold24h")
        rows.append({
            "market_hash_name": name,
            "date": today,
            "price": price,
            "volume": int(volume) if volume is not None else None,
            "source": "steamwebapi",
        })
        captured_names.append(name)

    await repo.upsert_prices(rows)
    await repo.mark_captured(captured_names, today)

    return {"tracked_run": len(names), "captured": len(rows),
            "skipped": skipped, "errors": errors}
```

- [ ] **Step 5: Verificar el seed contra la API viva y podar nombres inválidos**

Confirmá que cada nombre del seed resuelve (para no seguir nombres fantasma). Correr:

```bash
venv\Scripts\python -c "import os,json,httpx; from dotenv import load_dotenv; load_dotenv(); \
k=os.getenv('STEAM_API_KEY'); \
names=json.load(open('steam/data/tracked_seed.json',encoding='utf-8')); \
bad=[]; \
[bad.append(n) for n in names if (lambda r: not (isinstance(r,list) and r and r[0].get('pricelatestsell')) and not (isinstance(r,dict) and r.get('pricelatestsell')))(httpx.get('https://www.steamwebapi.com/steam/api/item',params={'key':k,'game':'cs2','market_hash_name':n,'format':'json'},timeout=25,follow_redirects=True).json())]; \
print('inválidos:', bad)"
```

Expected: idealmente `inválidos: []`. Si aparece alguno (nombre mal escrito), **quitarlo del JSON** y volver a correr hasta que la lista quede limpia. (Este paso gasta ~50 llamadas de la cuota, una sola vez.)

- [ ] **Step 6: Correr los tests para verlos pasar**

Run: `venv\Scripts\python -m pytest tests/test_price_capture.py -v`
Expected: PASS (8 passed).

- [ ] **Step 7: Commit local (sin push)**

```bash
git add steam/data/tracked_seed.json steam/price_capture.py tests/test_price_capture.py
git commit -m "feat(price): seed curado + captura diaria (lookup por-nombre, best-effort)"
```

---

## Task 4: Endpoint + auto-registro + seed en startup

**Files:**
- Modify: `steam/routes/market.py` (`POST /internal/price-tick`)
- Modify: `steam/routes/items.py` (auto-registro en `/inventory`)
- Modify: `main.py` (seed idempotente en el lifespan)
- Test: `tests/test_price_tick_router.py`

**Interfaces:**
- Consumes: `steam.price_capture` (`capture`, `seed_tracked`), `steam.price_history_repo.register_tracked`, `settings.PRICE_TICK_TOKEN`; `secrets`, `fastapi.Header`.
- Produces: `POST /internal/price-tick` (token `X-Price-Tick-Token`) → dict de `capture`. `/inventory` registra sus nombres (best-effort). Lifespan llama `seed_tracked()` (best-effort).

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/test_price_tick_router.py`:

```python
from unittest.mock import AsyncMock

from steam.routes import market as market_routes


def test_price_tick_requires_token(client, monkeypatch):
    monkeypatch.setattr(market_routes, "PRICE_TICK_TOKEN", "secret123")
    resp = client.post("/internal/price-tick")
    assert resp.status_code == 401


def test_price_tick_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr(market_routes, "PRICE_TICK_TOKEN", "secret123")
    resp = client.post("/internal/price-tick", headers={"X-Price-Tick-Token": "nope"})
    assert resp.status_code == 401


def test_price_tick_runs_with_valid_token(client, monkeypatch):
    monkeypatch.setattr(market_routes, "PRICE_TICK_TOKEN", "secret123")
    monkeypatch.setattr(market_routes, "price_capture_run",
                        AsyncMock(return_value={"tracked_run": 3, "captured": 2,
                                               "skipped": 1, "errors": 0}))
    resp = client.post("/internal/price-tick", headers={"X-Price-Tick-Token": "secret123"})
    assert resp.status_code == 200
    assert resp.json() == {"tracked_run": 3, "captured": 2, "skipped": 1, "errors": 0}
```

Nota: `market_routes.price_capture_run` es el alias importado en el Step 3 (`from steam.price_capture import capture as price_capture_run`). El `client` fixture vive en `tests/conftest.py`.

- [ ] **Step 2: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_price_tick_router.py -v`
Expected: FAIL (404: la ruta aún no existe).

- [ ] **Step 3: Añadir el endpoint en `steam/routes/market.py`**

Añadir imports (junto a los existentes del módulo):

```python
import secrets
from fastapi import Header

from settings import PRICE_TICK_TOKEN
from steam.price_capture import capture as price_capture_run
```

Añadir la ruta (junto a las demás `/internal/*` o al final del router):

```python
@router.post("/internal/price-tick", summary="Captura diaria de precios por-skin (cron)")
async def price_tick(
    request: Request,
    x_price_tick_token: str = Header(default=""),
):
    if not PRICE_TICK_TOKEN or not secrets.compare_digest(
        x_price_tick_token.encode(), PRICE_TICK_TOKEN.encode()
    ):
        raise HTTPException(status_code=401, detail="Token inválido")
    return await price_capture_run(request.app.state.http_client)
```

- [ ] **Step 4: Auto-registro en `/inventory` (`steam/routes/items.py`)**

En `_fetch_fresh_inventory` (que hace `items = [_map_item(item) for item in data]`, ~L111), registrar los nombres best-effort **antes** del `return items`. `_map_item` expone el market_hash_name bajo la clave `"name"` (`steam/mappers.py:196`). El módulo ya tiene `logger` (L28):

```python
    # Auto-registro para la captura de precios (best-effort: nunca romper /inventory)
    try:
        from steam.price_history_repo import register_tracked
        names = [i.get("name") for i in items if i.get("name")]
        if names:
            await register_tracked(names, "inventory")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[price] auto-registro de inventario falló: %s", exc)
```

(Se registra solo en fetch fresco — con el cache de 23 h, al menos una vez al día por usuario, suficiente.)

- [ ] **Step 5: Seed idempotente en el lifespan (`main.py`)**

En el `lifespan`, tras crear `app.state.http_client`, añadir (best-effort para no romper el arranque):

```python
    try:
        from steam.price_capture import seed_tracked
        await seed_tracked()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[price] seed inicial falló: %s", exc)
```

- [ ] **Step 6: Correr los tests + suite completa**

Run: `venv\Scripts\python -m pytest tests/test_price_tick_router.py -v`
Expected: PASS (3 passed).

Run: `venv\Scripts\python -m pytest tests/ -q`
Expected: toda la suite verde (los tests previos + los nuevos), sin romper `/inventory`.

- [ ] **Step 7: Commit local (sin push)**

```bash
git add steam/routes/market.py steam/routes/items.py main.py tests/test_price_tick_router.py
git commit -m "feat(price): endpoint /internal/price-tick + auto-registro en /inventory + seed en startup"
```

---

## Task 5: Cron de captura (GitHub Actions)

**Files:**
- Create: `.github/workflows/price-tick.yml`

**Interfaces:**
- Consumes: secrets `BACKEND_BASE_URL` y `PRICE_TICK_TOKEN`.

- [ ] **Step 1: Revisar `cap-tick.yml` como plantilla**

Run: `cat .github/workflows/cap-tick.yml`
Expected: ver el patrón (schedule + `curl -fsS` con header de token y env-var secrets).

- [ ] **Step 2: Crear `.github/workflows/price-tick.yml`**

Espejar el estilo de `cap-tick.yml` (env-var secrets, no URL hardcodeada):

```yaml
name: price-tick

# Cron diario que captura un snapshot de precios de las skins seguidas
# (tracked_skins) en precios_historicos. Idempotente (upsert por name+date).
# Despierta el backend en Render (free) aunque esté dormido.

on:
  schedule:
    - cron: "40 6 * * *"   # diario 06:40 UTC
  workflow_dispatch: {}

jobs:
  tick:
    runs-on: ubuntu-latest
    steps:
      - name: POST /internal/price-tick
        run: |
          curl -fsS --max-time 900 -X POST "$BASE_URL/internal/price-tick" \
            -H "X-Price-Tick-Token: $PRICE_TICK_TOKEN"
        env:
          BASE_URL: ${{ secrets.BACKEND_BASE_URL }}
          PRICE_TICK_TOKEN: ${{ secrets.PRICE_TICK_TOKEN }}
```

Nota: `--max-time 900` (15 min) porque la captura puede recorrer cientos de skins a ≤18/60s.

- [ ] **Step 3: Validar el YAML**

Run: `venv\Scripts\python -c "import yaml; yaml.safe_load(open('.github/workflows/price-tick.yml')); print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit local (sin push)**

```bash
git add .github/workflows/price-tick.yml
git commit -m "feat(price): workflow GitHub Actions price-tick (cron diario)"
```

---

## Cierre (manual, tras aprobar el operador)

No se ejecuta en esta sesión (sin push/merge):

1. Correr `docs/sql/precios_historicos.sql` en el SQL editor de Supabase (`cs-finance`).
2. Setear `PRICE_TICK_TOKEN` (`secrets.token_urlsafe(32)`) en `.env`, Render y GitHub Actions; confirmar `BACKEND_BASE_URL` en GH Actions.
3. (Opcional) Disparar `price-tick` a mano para el primer snapshot; verificar filas en `precios_historicos`.
4. El seed se auto-registra al arrancar el backend (o correr `seed_tracked` a mano).

---

## Self-Review (cobertura del spec)

- Tablas `tracked_skins` + `precios_historicos` + config → Task 1 ✅
- Capa Supabase (register/fetch/upsert/mark/count) → Task 2 ✅
- Seed curado (asset) + `seed_tracked` idempotente → Task 3 ✅
- Captura por-nombre vía limiter, tope, best-effort, precio canónico + volumen → Task 3 ✅
- Verificación de nombres del seed contra la API → Task 3 Step 5 ✅
- `POST /internal/price-tick` con token → Task 4 ✅
- Auto-registro en `/inventory` best-effort → Task 4 ✅
- Seed en startup → Task 4 ✅
- Cron GitHub Actions → Task 5 ✅
- Fuera de alcance (modelo, predicción, orquestador) → no se toca. ✅
