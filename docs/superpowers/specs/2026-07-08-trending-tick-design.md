# Trending Tick — Design Spec

**Date:** 2026-07-08
**Status:** Approved
**Scope:** Backend only (`LoginCsFinance`)

---

## Problem

`GET /market/trending` recalculates its result on every cache miss: up to 18 concurrent calls to `csfloat/history` (`_enrich_prices`) plus two calls to `market/{csfloat,buff}/prices` (`_enrich_market_prices`), against steamwebapi's Starter plan (20 req/60s per endpoint). The in-memory cache (`_trending_cache`, `TRENDING_CACHE_TTL`) is lost on every Render redeploy and isn't shared across workers, so the first request after a redeploy pays the full cost and risks hitting the rate limit if it overlaps with other endpoints sharing `_history_limiter`.

`market_cap_history` already solved an analogous problem for the CS2 price index: an external cron (GitHub Actions) hits an internal endpoint hourly, which fetches from steamwebapi once and persists to Supabase. The public endpoint then only reads from Supabase — no steamwebapi calls, no rate-limit exposure, survives redeploys.

**Why this doesn't fully replicate for trending:** `market_cap_history` persists a single time-series value per hour. `/market/trending` returns a list of ~18 items whose *composition* and prices change hour to hour, requiring the same expensive multi-source calculation regardless of who triggers it (a user request or a cron tick) — there's no cheaper way to know which items are "trending" this hour. This design does **not** attempt to eliminate that calculation cost or build a per-item price history; it moves the *existing* calculation off the request path and onto an hourly cron, and persists only the latest snapshot.

---

## Design

### New table: `public.market_trending`

One row per item, replacing the entire table contents on every tick (18 items — DELETE + INSERT is cheap and always reflects the exact current ranking, no orphaned rows to reconcile).

| Column | Type | Notes |
|---|---|---|
| `name` | text PK | `market_hash_name` — stable item identifier |
| `rank` | integer | Position in the ranking (0–17); Postgres doesn't guarantee row order without explicit `ORDER BY` |
| `slug` | text | |
| `weapon_type` | text | |
| `item_name` | text | |
| `item_type` | text | |
| `image` | text | |
| `rarity` | text | |
| `rarity_color` | text | |
| `border_color` | text | |
| `quality` | text | |
| `is_stat_trak` | boolean | |
| `is_souvenir` | boolean | |
| `is_star` | boolean | |
| `exterior` | text | nullable |
| `float_min` | numeric | nullable |
| `float_max` | numeric | nullable |
| `paint_index` | integer | nullable |
| `phase` | text | nullable |
| `price_latest` | numeric | |
| `csfloat_price` | numeric | nullable |
| `buff_price` | numeric | nullable |
| `price_delta_24h` | numeric | nullable |
| `price_delta_7d` | numeric | nullable |
| `price_delta_30d` | numeric | nullable |
| `updated_at` | timestamptz | set on every tick |

RLS enabled, no policies — same as `market_cap_history`; backend accesses via `service_role`, bypassing RLS.

### `steam/trending_repo.py` (new)

Same shape as `cap_history_repo.py`: module-cached Supabase client (reuse `get_supabase()` from `cap_history_repo.py` rather than duplicating it — extract to a shared `steam/supabase_client.py` if that's cleaner, otherwise import it directly), sync `supabase-py` calls wrapped in `asyncio.to_thread`.

```python
async def replace_snapshot(items: list[dict]) -> None:
    """Replaces the entire table contents with the current ranking."""
    def _do() -> None:
        client = get_supabase()
        client.table(_TABLE).delete().neq("name", "").execute()  # delete all
        if items:
            client.table(_TABLE).insert(items).execute()
    await asyncio.to_thread(_do)

async def fetch_snapshot() -> list[dict]:
    """All rows ordered by rank ascending."""
    def _do() -> list[dict]:
        resp = get_supabase().table(_TABLE).select("*").order("rank", desc=False).execute()
        return resp.data or []
    return await asyncio.to_thread(_do)
```

Delete-all-then-insert isn't atomic across two statements in supabase-py's REST interface, but the table is only ever read by `GET /market/trending`, and a brief empty window during a tick (worst case: one request sees `[]` for a few hundred ms) is an acceptable tradeoff for simplicity — matches the "borrar todo y reinsertar" decision.

### `steam/routes/market.py` changes

1. Extract the body of `get_market_trending` (lines 249–343: primary `/items` source, topmovers fallback, stale-cache fallback) into a private async function `_compute_trending(client: httpx.AsyncClient) -> list[dict]` that returns the computed list. No behavior change — same sources, same fallback order, same `_TRENDING_LIMIT`.

2. `GET /market/trending` becomes:
   ```python
   @router.get("/market/trending", ...)
   async def get_market_trending(request: Request, user: dict = Depends(require_jwt)):
       return await fetch_snapshot()
   ```
   No steamwebapi calls, no in-memory cache — Supabase is the sole source. `_trending_cache` and `TRENDING_CACHE_TTL` become dead code, removed along with their import in `stores.py`.

3. New endpoint, next to `cap_tick`:
   ```python
   @router.post("/internal/trending-tick", summary="Captura el ranking trending del mercado CS2 (cron interno)")
   async def trending_tick(request: Request, x_cap_token: str | None = Header(default=None)):
       if not CAP_TICK_TOKEN or not x_cap_token or not secrets.compare_digest(x_cap_token, CAP_TICK_TOKEN):
           raise HTTPException(status_code=401, detail="Invalid or missing cap-tick token")
       items = await _compute_trending(request.app.state.http_client)
       rows = [_to_row(item, rank) for rank, item in enumerate(items)]
       await replace_snapshot(rows)
       logger.info("[trending-tick] snapshot saved: %d items", len(rows))
       return {"ok": True, "count": len(rows)}
   ```
   Reuses `CAP_TICK_TOKEN` (already a generic internal-cron secret, not cap-history-specific) — no new env var.

4. `_to_row(item, rank)`: maps the camelCase `ISkinCard`-shaped dict (`priceDelta7d`, `weaponType`, etc.) to the snake_case DB columns above, adding `rank` and `updated_at`.

### `.github/workflows/cap-tick.yml`

Add a second sequential step after the existing `cap-tick` call:

```yaml
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

Same cron (`5 * * * *`), same `workflow_dispatch` trigger. If `trending-tick` fails, `cap-tick` (which already ran) is unaffected — sequential `curl -fsS` steps fail independently; a failure in step 2 doesn't roll back step 1.

Rename the workflow file's top comment to reflect both ticks, or leave as-is and let the step names self-document — no functional impact either way (deferred to implementation).

---

## Data flow (after this change)

```
GitHub Actions (hourly, :05)
  → POST /internal/cap-tick        → upsert market_cap_history (by ts)
  → POST /internal/trending-tick   → replace market_trending (all rows)

GET /market/trending  → fetch_snapshot() → Supabase only, no steamwebapi call
GET /market/cap-history → fetch_range() → Supabase only, no steamwebapi call (unchanged)
```

All other endpoints (`/market/index`, `/market/movers`, `/inventory`, `/me`, `/item/history`) are untouched — they keep using in-memory caches + direct steamwebapi calls, which remains correct for per-user or infrequently-changing data.

---

## Out of scope

- No per-item price history table — rejected during design (see conversation): the trending candidate set changes hour to hour, so pre-populating a history wouldn't reduce `csfloat/history` calls, only add unbounded storage growth.
- No change to `/market/movers`, `_MOVERS_SELECT`, `_MOVERS_LIMIT`, or `_history_limiter`.
- No change to `_TRENDING_LIMIT` (stays 18).
- No new environment variable — reuses `CAP_TICK_TOKEN`.

---

## Testing

- Unit test for `_to_row` mapping (camelCase → snake_case, `rank` assignment).
- Manual verification: `workflow_dispatch` trigger to run the tick on demand, confirm `market_trending` table populates, confirm `GET /market/trending` serves from it with no steamwebapi calls in the logs.
- No automated test framework configured in this repo (per `CLAUDE.md`) — matches existing project convention of manual verification for backend changes.
