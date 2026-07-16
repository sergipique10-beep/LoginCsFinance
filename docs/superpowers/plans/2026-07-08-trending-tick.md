# Trending Tick Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the `/market/trending` ranking in Supabase, refreshed hourly by a cron-triggered internal endpoint, so the public endpoint no longer recalculates it (up to 18 `csfloat/history` calls) on every cache miss.

**Architecture:** Same pattern as `market_cap_history`/`cap_history_repo.py`: a new `market_trending` table (one row per item, replaced wholesale on each tick), a `steam/trending_repo.py` data layer, a `POST /internal/trending-tick` endpoint reusing `CAP_TICK_TOKEN`, and `GET /market/trending` simplified to a pure Supabase read. `cap-tick.yml` gains a second sequential curl step.

**Tech Stack:** Python 3.12, FastAPI, httpx (async), supabase-py (sync, wrapped in `asyncio.to_thread`), GitHub Actions.

## Global Constraints

- No test framework configured in this repo — verification is manual (`curl` / `workflow_dispatch` + log inspection), per project convention (see `CLAUDE.md`: "There are no test or lint commands configured").
- Reuse `CAP_TICK_TOKEN` — no new environment variable.
- `_TRENDING_LIMIT` stays 18. No change to `/market/movers`, `_MOVERS_SELECT`, `_MOVERS_LIMIT`, or `_history_limiter`.
- All Supabase writes go through `service_role` (bypasses RLS) — never the anon key.
- `app.state.http_client` is the only HTTP client to use for external calls — never create a per-request client.
- Table replacement strategy: DELETE all rows + INSERT new rows on every tick (not incremental upsert/diff) — approved in spec.

---

## File Structure

- `steam/trending_repo.py` — **new**. Supabase data layer for `market_trending`: `replace_snapshot`, `fetch_snapshot`.
- `steam/routes/market.py` — **modify**. Extract `_compute_trending`, simplify `GET /market/trending`, add `POST /internal/trending-tick`, add `_to_row` mapper.
- `stores.py` — **modify**. Remove `TRENDING_CACHE_TTL` and `_trending_cache` (dead after migration).
- `.github/workflows/cap-tick.yml` — **modify**. Add second step for `POST /internal/trending-tick`.
- `docs/superpowers/specs/2026-07-08-trending-tick-design.md` — reference, already committed, no changes.

Supabase table `public.market_trending` is created via the Supabase SQL editor (or CLI migration if the project uses one — see Task 1) — not a repo file, but documented in Task 1.

---

### Task 1: Create the `market_trending` table in Supabase

**Files:** none (Supabase-side DDL, run manually via SQL editor or CLI)

**Interfaces:**
- Consumes: nothing.
- Produces: `public.market_trending` table, consumed by `steam/trending_repo.py` (Task 2).

- [ ] **Step 1: Run the DDL in the Supabase SQL editor**

Open the `cs-finance` Supabase project → SQL Editor → run:

```sql
create table public.market_trending (
  name text primary key,
  rank integer not null,
  slug text,
  weapon_type text,
  item_name text,
  item_type text,
  image text,
  rarity text,
  rarity_color text,
  border_color text,
  quality text,
  is_stat_trak boolean not null default false,
  is_souvenir boolean not null default false,
  is_star boolean not null default false,
  exterior text,
  float_min numeric,
  float_max numeric,
  paint_index integer,
  phase text,
  price_latest numeric not null default 0,
  csfloat_price numeric,
  buff_price numeric,
  price_delta_24h numeric,
  price_delta_7d numeric,
  price_delta_30d numeric,
  updated_at timestamptz not null default now()
);

alter table public.market_trending enable row level security;
```

No policies are added — matches `market_cap_history` (backend uses `service_role`, which bypasses RLS; no client-side access needed).

- [ ] **Step 2: Verify the table exists**

In the SQL editor, run:

```sql
select count(*) from public.market_trending;
```

Expected: returns `0` (empty table, no error).

---

### Task 2: `steam/trending_repo.py` — Supabase data layer

**Files:**
- Create: `steam/trending_repo.py`

**Interfaces:**
- Consumes: `get_supabase()` from `steam/cap_history_repo.py` (existing, module-cached Supabase client).
- Produces: `replace_snapshot(rows: list[dict]) -> None`, `fetch_snapshot() -> list[dict]`, consumed by `steam/routes/market.py` (Task 3).

- [ ] **Step 1: Write the module**

Create `steam/trending_repo.py`:

```python
"""
Persistencia del ranking trending del mercado CS2 en Supabase (Postgres).

Mismo patrón que cap_history_repo.py: la tabla `public.market_trending`
se reemplaza por completo en cada tick (DELETE + INSERT), reflejando
siempre el ranking exacto del último cron. No hay historial — solo el
snapshot más reciente.

supabase-py es síncrono → todas las llamadas se envuelven en asyncio.to_thread
para no bloquear el event loop.
"""
import asyncio
import logging

from .cap_history_repo import get_supabase

logger = logging.getLogger("uvicorn.error")

_TABLE = "market_trending"


async def replace_snapshot(rows: list[dict]) -> None:
    """Reemplaza el contenido completo de la tabla con el ranking actual."""
    def _do() -> None:
        client = get_supabase()
        client.table(_TABLE).delete().neq("name", "").execute()
        if rows:
            client.table(_TABLE).insert(rows).execute()

    await asyncio.to_thread(_do)


async def fetch_snapshot() -> list[dict]:
    """Todas las filas, ordenadas por rank ascendente."""
    def _do() -> list[dict]:
        resp = (
            get_supabase()
            .table(_TABLE)
            .select("*")
            .order("rank", desc=False)
            .execute()
        )
        return resp.data or []

    return await asyncio.to_thread(_do)
```

- [ ] **Step 2: Verify the module imports cleanly**

Run: `python -c "from steam.trending_repo import replace_snapshot, fetch_snapshot; print('ok')"`
Expected: prints `ok` with no import errors. (Run from the `LoginCsFinance/` directory with the venv active.)

- [ ] **Step 3: Commit**

```bash
git add steam/trending_repo.py
git commit -m "feat: add Supabase data layer for market_trending snapshot"
```

---

### Task 3: Extract `_compute_trending` from `get_market_trending`

**Files:**
- Modify: `steam/routes/market.py:248-343` (current `get_market_trending` body)

**Interfaces:**
- Consumes: `request.app.state.http_client` (existing pattern), all helpers already imported in `market.py` (`_map_item`, `_enrich_prices`, `_enrich_market_prices`, `_fetch_static_images`, `_enrich_images_from_cache`, `_cache_images`, `_map_topmovers_item`, `_category_rank`, `_topmovers_raw_cache`).
- Produces: `_compute_trending(client: httpx.AsyncClient) -> list[dict]`, consumed by `GET /market/trending` (this task) and `POST /internal/trending-tick` (Task 5).

This task is a pure refactor — no behavior change. The existing endpoint logic (primary `/items` source → topmovers fallback → stale-cache fallback) becomes a standalone function; the route handler becomes a thin wrapper during this task, then gets replaced entirely in Task 4.

- [ ] **Step 1: Read the current endpoint to confirm line numbers haven't drifted**

Run: `grep -n "async def get_market_trending" steam/routes/market.py`
Expected: `249:async def get_market_trending(request: Request, user: dict = Depends(require_jwt)):` (or close — if the line number differs, adjust the replacement range below accordingly, the function body is delimited by the blank lines before `@router.get("/market/index"` which follows it).

- [ ] **Step 2: Replace the function**

Current code (`steam/routes/market.py:248-343`):

```python
@router.get("/market/trending", summary="Items trending del mercado CS2 (por volumen 24h)")
async def get_market_trending(request: Request, user: dict = Depends(require_jwt)):
    cache_key = "trending"
    now = time.monotonic()
    cached = _trending_cache.get(cache_key)
    if cached and now - cached[1] < TRENDING_CACHE_TTL:
        return cached[0]

    # ── Primary source: /items (paid plan) ───────────────────────────────────
    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/items",
            params={
                "key": STEAM_API_KEY,
                "game": "cs2",
                "sort_by": "soldZa",
                "max": 150,
                "select": _MOVERS_SELECT,
                "format": "json",
                "production": "1",
            },
            timeout=15.0,
        )
        items_ok = resp.status_code == 200
    except (httpx.TimeoutException, httpx.RequestError):
        items_ok = False
        resp = None

    if items_ok and resp is not None:
        data = resp.json()
        if isinstance(data, list):
            _cache_images(data)
            result = []
            for raw in data:
                latest = float(raw.get("pricelatestsell") or 0)
                volume = int(raw.get("sold24h") or 0)
                if latest > 0 and volume >= 1:
                    result.append(_map_item(raw))
            result = sorted(
                result,
                key=lambda x: (_category_rank(x.get("weaponType")), -(x.get("sold24h") or 0)),
            )[:_TRENDING_LIMIT]
            result = await _enrich_prices(request.app.state.http_client, result)
            result = await _enrich_market_prices(request.app.state.http_client, result)
            # steamwebapi /items no devuelve `image` en este plan → el cache estático
            # (ByMykel) es la única fuente. Igual que en /market/items (search).
            await _fetch_static_images(request.app.state.http_client)
            _enrich_images_from_cache(result)
            _trending_cache[cache_key] = (result, now)
            return result
        logger.warning("[market-trending] /items returned unexpected type: %s", type(data).__name__)
    else:
        if resp is not None:
            logger.warning("[market-trending] /items returned %s — falling back to topmovers", resp.status_code)

    # ── Fallback: topmovers from cache (free plan) ────────────────────────────
    raw_topmovers = _topmovers_raw_cache.get("latest")
    if not raw_topmovers:
        try:
            mi_resp = await request.app.state.http_client.get(
                f"{STEAM_WEB_API}/market-index/cs2",
                params={"key": STEAM_API_KEY, "format": "json"},
                timeout=15.0,
            )
            if mi_resp.status_code == 200:
                mi_data = mi_resp.json()
                if isinstance(mi_data, dict):
                    tm = mi_data.get("topmovers", {})
                    gainers = tm.get("gainers", [])
                    losers  = tm.get("losers", [])
                    _topmovers_raw_cache["latest"] = (gainers, losers, now)
                    raw_topmovers = _topmovers_raw_cache["latest"]
        except Exception as exc:
            logger.warning("[market-trending] could not fetch market-index for topmovers: %s", exc)

    if raw_topmovers:
        gainers, losers, _ = raw_topmovers
        combined = gainers + losers
        if combined:
            await _fetch_static_images(request.app.state.http_client)
            result = [_map_topmovers_item(item) for item in combined]
            _enrich_images_from_cache(result)
            result = sorted(result, key=lambda x: x["sold24h"], reverse=True)[:_TRENDING_LIMIT]
            result = await _enrich_market_prices(request.app.state.http_client, result)
            _trending_cache[cache_key] = (result, now)
            logger.info("[market-trending] serving from topmovers (%d items)", len(result))
            return result

    # ── Stale cache as last resort ────────────────────────────────────────────
    stale = _trending_cache.get(cache_key)
    if stale:
        logger.info("[market-trending] serving stale cache (%.0f s old)", now - stale[1])
        return stale[0]

    logger.warning("[market-trending] no data available from any source")
    return []
```

Replace with:

```python
async def _compute_trending(client: httpx.AsyncClient) -> list[dict]:
    """Calcula el ranking trending actual (sin cache, sin persistencia).

    Llamado tanto por GET /market/trending (antes de la migración a
    Supabase) como por POST /internal/trending-tick.
    """
    now = time.monotonic()

    # ── Primary source: /items (paid plan) ───────────────────────────────────
    try:
        resp = await client.get(
            f"{STEAM_WEB_API}/items",
            params={
                "key": STEAM_API_KEY,
                "game": "cs2",
                "sort_by": "soldZa",
                "max": 150,
                "select": _MOVERS_SELECT,
                "format": "json",
                "production": "1",
            },
            timeout=15.0,
        )
        items_ok = resp.status_code == 200
    except (httpx.TimeoutException, httpx.RequestError):
        items_ok = False
        resp = None

    if items_ok and resp is not None:
        data = resp.json()
        if isinstance(data, list):
            _cache_images(data)
            result = []
            for raw in data:
                latest = float(raw.get("pricelatestsell") or 0)
                volume = int(raw.get("sold24h") or 0)
                if latest > 0 and volume >= 1:
                    result.append(_map_item(raw))
            result = sorted(
                result,
                key=lambda x: (_category_rank(x.get("weaponType")), -(x.get("sold24h") or 0)),
            )[:_TRENDING_LIMIT]
            result = await _enrich_prices(client, result)
            result = await _enrich_market_prices(client, result)
            # steamwebapi /items no devuelve `image` en este plan → el cache estático
            # (ByMykel) es la única fuente. Igual que en /market/items (search).
            await _fetch_static_images(client)
            _enrich_images_from_cache(result)
            return result
        logger.warning("[market-trending] /items returned unexpected type: %s", type(data).__name__)
    else:
        if resp is not None:
            logger.warning("[market-trending] /items returned %s — falling back to topmovers", resp.status_code)

    # ── Fallback: topmovers from cache (free plan) ────────────────────────────
    raw_topmovers = _topmovers_raw_cache.get("latest")
    if not raw_topmovers:
        try:
            mi_resp = await client.get(
                f"{STEAM_WEB_API}/market-index/cs2",
                params={"key": STEAM_API_KEY, "format": "json"},
                timeout=15.0,
            )
            if mi_resp.status_code == 200:
                mi_data = mi_resp.json()
                if isinstance(mi_data, dict):
                    tm = mi_data.get("topmovers", {})
                    gainers = tm.get("gainers", [])
                    losers  = tm.get("losers", [])
                    _topmovers_raw_cache["latest"] = (gainers, losers, now)
                    raw_topmovers = _topmovers_raw_cache["latest"]
        except Exception as exc:
            logger.warning("[market-trending] could not fetch market-index for topmovers: %s", exc)

    if raw_topmovers:
        gainers, losers, _ = raw_topmovers
        combined = gainers + losers
        if combined:
            await _fetch_static_images(client)
            result = [_map_topmovers_item(item) for item in combined]
            _enrich_images_from_cache(result)
            result = sorted(result, key=lambda x: x["sold24h"], reverse=True)[:_TRENDING_LIMIT]
            result = await _enrich_market_prices(client, result)
            logger.info("[market-trending] serving from topmovers (%d items)", len(result))
            return result

    logger.warning("[market-trending] no data available from any source")
    return []


@router.get("/market/trending", summary="Items trending del mercado CS2 (por volumen 24h)")
async def get_market_trending(request: Request, user: dict = Depends(require_jwt)):
    return await _compute_trending(request.app.state.http_client)
```

Note: the in-memory cache reads/writes (`_trending_cache`) and the stale-cache fallback are removed from `_compute_trending` — they're handled by Supabase persistence in Task 5/6, not by this function. This is intentional: `_compute_trending` is now a pure calculation, no caching side effects. The route handler in this task still calls it directly (no Supabase yet) — Task 4 replaces the route body to read from Supabase instead.

- [ ] **Step 3: Start the dev server and manually verify**

Run: `python main.py` (or `uvicorn main:app --host 127.0.0.1 --port 8000 --reload`)

In another terminal, get a dev token (requires `DEBUG=true` in `.env`) and call the endpoint:

```bash
curl -s http://localhost:8000/auth/dev-token | head -c 200
# copy the access_token, then:
curl -s http://localhost:8000/market/trending -H "Authorization: Bearer <token>" | head -c 500
```

Expected: a JSON array of up to 18 items with `name`, `priceLatest`, `priceDelta7d`, etc. — same shape as before this task (behavior unchanged, just refactored). Check the server logs for `[market-trending]` lines confirming which source was used.

- [ ] **Step 4: Commit**

```bash
git add steam/routes/market.py
git commit -m "refactor: extract _compute_trending from GET /market/trending"
```

---

### Task 4: `GET /market/trending` reads from Supabase

**Files:**
- Modify: `steam/routes/market.py` (import + route handler)

**Interfaces:**
- Consumes: `fetch_snapshot()` from `steam/trending_repo.py` (Task 2).
- Produces: `GET /market/trending` now returns Supabase data only.

- [ ] **Step 1: Add the import**

In `steam/routes/market.py`, find the existing import line:

```python
from ..cap_history_repo import insert_snapshot, fetch_range
```

Add immediately after it:

```python
from ..trending_repo import replace_snapshot, fetch_snapshot
```

- [ ] **Step 2: Replace the route handler**

Current (from Task 3):

```python
@router.get("/market/trending", summary="Items trending del mercado CS2 (por volumen 24h)")
async def get_market_trending(request: Request, user: dict = Depends(require_jwt)):
    return await _compute_trending(request.app.state.http_client)
```

Replace with:

```python
@router.get("/market/trending", summary="Items trending del mercado CS2 (por volumen 24h)")
async def get_market_trending(request: Request, user: dict = Depends(require_jwt)):
    rows = await fetch_snapshot()
    return [_row_to_item(row) for row in rows]
```

This introduces `_row_to_item`, the inverse of `_to_row` (Task 5) — converts snake_case DB columns back to the camelCase `ISkinCard` shape the frontend expects. Add it directly above the route handler:

```python
def _row_to_item(row: dict) -> dict:
    """Convierte una fila de market_trending (snake_case) al shape ISkinCard (camelCase)."""
    return {
        "id": row["name"],
        "name": row["name"],
        "slug": row.get("slug", ""),
        "weaponType": row.get("weapon_type"),
        "itemName": row.get("item_name"),
        "itemType": row.get("item_type"),
        "image": row.get("image", ""),
        "rarity": row.get("rarity", "Base Grade"),
        "rarityColor": row.get("rarity_color", "b0c3d9"),
        "borderColor": row.get("border_color", "b0c3d9"),
        "quality": row.get("quality", "Normal"),
        "isStatTrak": row.get("is_stat_trak", False),
        "isSouvenir": row.get("is_souvenir", False),
        "isStar": row.get("is_star", False),
        "exterior": row.get("exterior"),
        "floatValue": None,
        "floatMin": row.get("float_min"),
        "floatMax": row.get("float_max"),
        "paintIndex": row.get("paint_index"),
        "phase": row.get("phase"),
        "priceLatest": row.get("price_latest", 0),
        "csfloatPrice": row.get("csfloat_price"),
        "buffPrice": row.get("buff_price"),
        "priceSafe": 0,
        "priceMin": 0,
        "priceMax": 0,
        "priceDelta24h": row.get("price_delta_24h"),
        "priceDelta7d": row.get("price_delta_7d"),
        "priceDelta30d": row.get("price_delta_30d"),
    }
```

- [ ] **Step 3: Verify the endpoint returns an empty list (table is empty until Task 5/6 populate it)**

With the dev server running (from Task 3):

```bash
curl -s http://localhost:8000/market/trending -H "Authorization: Bearer <token>" | head -c 200
```

Expected: `[]` — the table is still empty at this point, no steamwebapi calls happen (check server logs for absence of `[market-trending]` lines, since `_compute_trending` is no longer called from this route).

- [ ] **Step 4: Commit**

```bash
git add steam/routes/market.py
git commit -m "feat: GET /market/trending reads from Supabase market_trending"
```

---

### Task 5: `POST /internal/trending-tick`

**Files:**
- Modify: `steam/routes/market.py`

**Interfaces:**
- Consumes: `_compute_trending` (Task 3), `replace_snapshot` (Task 2/4 import), `CAP_TICK_TOKEN` (already imported from `settings`).
- Produces: `POST /internal/trending-tick` endpoint, called by the cron (Task 6).

- [ ] **Step 1: Add `_to_row` and the endpoint**

In `steam/routes/market.py`, add near the existing `cap_tick` endpoint (after it, or before — either position works since there's no dependency order):

```python
def _to_row(item: dict, rank: int) -> dict:
    """Convierte un item ISkinCard-shaped (camelCase) a una fila de market_trending (snake_case)."""
    return {
        "name": item["name"],
        "rank": rank,
        "slug": item.get("slug", ""),
        "weapon_type": item.get("weaponType"),
        "item_name": item.get("itemName"),
        "item_type": item.get("itemType"),
        "image": item.get("image", ""),
        "rarity": item.get("rarity", "Base Grade"),
        "rarity_color": item.get("rarityColor", "b0c3d9"),
        "border_color": item.get("borderColor", "b0c3d9"),
        "quality": item.get("quality", "Normal"),
        "is_stat_trak": bool(item.get("isStatTrak", False)),
        "is_souvenir": bool(item.get("isSouvenir", False)),
        "is_star": bool(item.get("isStar", False)),
        "exterior": item.get("exterior"),
        "float_min": item.get("floatMin"),
        "float_max": item.get("floatMax"),
        "paint_index": item.get("paintIndex"),
        "phase": item.get("phase"),
        "price_latest": item.get("priceLatest", 0),
        "csfloat_price": item.get("csfloatPrice"),
        "buff_price": item.get("buffPrice"),
        "price_delta_24h": item.get("priceDelta24h"),
        "price_delta_7d": item.get("priceDelta7d"),
        "price_delta_30d": item.get("priceDelta30d"),
    }


@router.post("/internal/trending-tick", summary="Captura el ranking trending del mercado CS2 (cron interno)")
async def trending_tick(
    request: Request,
    x_cap_token: str | None = Header(default=None),
):
    if not CAP_TICK_TOKEN or not x_cap_token or not secrets.compare_digest(x_cap_token, CAP_TICK_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing cap-tick token")

    items = await _compute_trending(request.app.state.http_client)
    rows = [_to_row(item, rank) for rank, item in enumerate(items)]
    await replace_snapshot(rows)
    logger.info("[trending-tick] snapshot saved: %d items", len(rows))
    return {"ok": True, "count": len(rows)}
```

Note: `Header`, `HTTPException`, `secrets` are already imported at the top of `market.py` (used by `cap_tick`) — no new imports needed for this step beyond the ones added in Task 4.

- [ ] **Step 2: Verify with the dev server running**

```bash
curl -s -X POST http://localhost:8000/internal/trending-tick -H "X-Cap-Token: <your CAP_TICK_TOKEN from .env>"
```

Expected: `{"ok":true,"count":<N>}` where N is up to 18. Check server logs for `[trending-tick] snapshot saved: N items` and the `[market-trending]` source logs from `_compute_trending`.

- [ ] **Step 3: Verify the table was populated**

In the Supabase SQL editor:

```sql
select name, rank, price_latest, price_delta_7d from public.market_trending order by rank limit 5;
```

Expected: up to 5 rows with `rank` 0–4, non-empty `name`, non-zero `price_latest`.

- [ ] **Step 4: Verify `GET /market/trending` now serves this data**

```bash
curl -s http://localhost:8000/market/trending -H "Authorization: Bearer <token>" | head -c 500
```

Expected: a JSON array of items (no longer `[]`), matching the rows just inserted. Server logs should show **no** `[market-trending]` source lines for this call (confirms it's reading from Supabase, not recalculating).

- [ ] **Step 5: Verify auth rejection**

```bash
curl -s -X POST http://localhost:8000/internal/trending-tick -H "X-Cap-Token: wrong-token" -o /dev/null -w "%{http_code}\n"
```

Expected: `401`

- [ ] **Step 6: Commit**

```bash
git add steam/routes/market.py
git commit -m "feat: add POST /internal/trending-tick endpoint"
```

---

### Task 6: Remove dead in-memory cache code

**Files:**
- Modify: `stores.py`
- Modify: `steam/routes/market.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing (cleanup only).

- [ ] **Step 1: Remove `TRENDING_CACHE_TTL` and `_trending_cache` from `stores.py`**

In `stores.py`, remove this line from the constants section:

```python
TRENDING_CACHE_TTL = 82800   # 23 h — same daily budget
```

And this line from the stores section:

```python
_trending_cache: dict[str, tuple[list, float]] = {}
```

- [ ] **Step 2: Remove the now-unused import from `steam/routes/market.py`**

Find:

```python
from stores import (
    MARKET_INDEX_CACHE_TTL, MOVERS_CACHE_TTL, TRENDING_CACHE_TTL,
    SEARCH_CACHE_TTL, MARKET_PRICES_CACHE_TTL,
    _market_index_cache, _movers_cache, _topmovers_raw_cache,
    _trending_cache, _search_cache, _market_prices_cache,
)
```

Replace with:

```python
from stores import (
    MARKET_INDEX_CACHE_TTL, MOVERS_CACHE_TTL,
    SEARCH_CACHE_TTL, MARKET_PRICES_CACHE_TTL,
    _market_index_cache, _movers_cache, _topmovers_raw_cache,
    _search_cache, _market_prices_cache,
)
```

- [ ] **Step 3: Verify the module still imports cleanly**

Run: `python -c "import steam.routes.market; print('ok')"`
Expected: prints `ok`, no `NameError` or `ImportError`.

- [ ] **Step 4: Restart the dev server and re-verify `GET /market/trending` still works**

```bash
curl -s http://localhost:8000/market/trending -H "Authorization: Bearer <token>" | head -c 200
```

Expected: same JSON array as Task 5 Step 4 (data persisted in Supabase, unaffected by removing the in-memory cache).

- [ ] **Step 5: Commit**

```bash
git add stores.py steam/routes/market.py
git commit -m "chore: remove unused in-memory trending cache"
```

---

### Task 7: Update the GitHub Actions workflow

**Files:**
- Modify: `.github/workflows/cap-tick.yml`

**Interfaces:**
- Consumes: `POST /internal/trending-tick` (Task 5), existing secrets `BACKEND_BASE_URL` and `CAP_TICK_TOKEN`.
- Produces: hourly automated trigger for the new endpoint.

- [ ] **Step 1: Read the current file**

Current `.github/workflows/cap-tick.yml`:

```yaml
name: cap-tick

# Cron externo que despierta al backend (Render free duerme) y captura un
# snapshot horario del índice de precio CS2. El endpoint es idempotente por
# hora (upsert por ts en Supabase), así que reintentos no duplican filas.

on:
  schedule:
    - cron: "5 * * * *"
  workflow_dispatch: {}

jobs:
  tick:
    runs-on: ubuntu-latest
    steps:
      - name: POST /internal/cap-tick
        run: |
          curl -fsS -X POST "$BASE_URL/internal/cap-tick" -H "X-Cap-Token: $CAP_TICK_TOKEN"
        env:
          BASE_URL: ${{ secrets.BACKEND_BASE_URL }}
          CAP_TICK_TOKEN: ${{ secrets.CAP_TICK_TOKEN }}
```

- [ ] **Step 2: Replace with the two-step version**

```yaml
name: cap-tick

# Cron externo que despierta al backend (Render free duerme) y captura
# snapshots horarios: el índice de precio CS2 (cap-tick, upsert por ts) y
# el ranking trending del mercado (trending-tick, reemplazo completo).
# Ambos endpoints son idempotentes/deterministas dentro de la misma hora,
# así que reintentos no duplican ni corrompen datos.

on:
  schedule:
    - cron: "5 * * * *"
  workflow_dispatch: {}

jobs:
  tick:
    runs-on: ubuntu-latest
    steps:
      - name: POST /internal/cap-tick
        run: |
          curl -fsS -X POST "$BASE_URL/internal/cap-tick" -H "X-Cap-Token: $CAP_TICK_TOKEN"
        env:
          BASE_URL: ${{ secrets.BACKEND_BASE_URL }}
          CAP_TICK_TOKEN: ${{ secrets.CAP_TICK_TOKEN }}
      - name: POST /internal/trending-tick
        run: |
          curl -fsS -X POST "$BASE_URL/internal/trending-tick" -H "X-Cap-Token: $CAP_TICK_TOKEN"
        env:
          BASE_URL: ${{ secrets.BACKEND_BASE_URL }}
          CAP_TICK_TOKEN: ${{ secrets.CAP_TICK_TOKEN }}
```

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/cap-tick.yml
git commit -m "feat: trigger trending-tick from the hourly cap-tick workflow"
```

- [ ] **Step 4: Push and manually trigger the workflow**

```bash
git push
```

Then in the GitHub repo → Actions → `cap-tick` workflow → "Run workflow" (workflow_dispatch). Wait for it to complete.

Expected: both steps show green checkmarks. If `trending-tick` fails, `cap-tick` (already run) is unaffected — sequential steps fail independently.

- [ ] **Step 5: Verify against the deployed backend**

```bash
curl -s https://<your-render-backend-url>/market/trending -H "Authorization: Bearer <token>" | head -c 500
```

Expected: a populated JSON array, confirming the deployed workflow successfully ticked the production Supabase table.

---

### Task 8: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Confirm no steamwebapi calls happen on `GET /market/trending`**

With the dev server running and the table populated (from Task 5/7), tail the logs while calling the endpoint 3 times in a row:

```bash
for i in 1 2 3; do curl -s http://localhost:8000/market/trending -H "Authorization: Bearer <token>" -o /dev/null -w "%{http_code}\n"; done
```

Expected: three `200` responses, and **zero** `[market-trending]` log lines in the server output for any of the three calls (they all read from Supabase, not steamwebapi).

- [ ] **Step 2: Confirm the frontend still renders trending items correctly**

Per the root `CLAUDE.md` UI verification requirement: start the frontend (`cd ../CS-FINANCE-ionic && npm start`), log in, navigate to the Market tab, and visually confirm the trending list renders with images, prices, and delta badges — same as before this change. This confirms `_row_to_item`'s camelCase shape matches what `ISkinCard`/`TrendCard` expect on the frontend side.

- [ ] **Step 3: Document completion**

No further steps — the migration is complete. `market_trending` now refreshes hourly via cron; `GET /market/trending` never calls steamwebapi directly.
