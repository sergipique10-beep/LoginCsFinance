# Movers Enrichment Fix — Design Spec

**Date:** 2026-06-23
**Status:** Approved
**Scope:** Backend only (`LoginCsfinance`)

---

## Problem

The `/market/movers` endpoint returns `priceDelta7d: null` for hot movers, causing the frontend to display "N/A" in all delta badges. Root cause has three layers:

1. `_map_item` intentionally sets all deltas to `null` — the only enrichment path is `_enrich_prices`, which calls `/history` per item.
2. `MOVERS_CACHE_TTL` was 23 hours — designed for the free plan (5 req/day). Once a failed enrichment (all-null deltas) is cached, it serves broken data for nearly a day.
3. The sort puts `null`-delta items last; `hot = reversed(by_delta[-10:])` picks exactly from that tail, so hot movers end up entirely null-delta when enrichment fails.

**Context:** The API is on the Starter plan — 20 req/min, 2,000/day. The 23h TTL and 5-concurrency assumptions were valid for the old free plan but are wrong now.

---

## Design

### Three targeted changes — no architectural additions

#### 1. `MOVERS_CACHE_TTL`: 82800 s → 900 s (`stores.py`)

Reduce from 23 hours to 15 minutes. Rationale: with 2,000 req/day the cache no longer needs to protect a daily quota. 15 minutes is a reasonable freshness window for market movers data.

No other TTLs change.

#### 2. Candidates cap: 30 → 20 (`steam/routes/market.py`)

Hot + cold display exactly `_MOVERS_LIMIT * 2 = 20` items. Reducing candidates to 20 means `_enrich_prices` makes exactly 20 `/history` calls per enrichment cycle. With `concurrency=5` (unchanged), this produces 4 sequential batches of 5 concurrent requests. At typical response times of 2–4 s per call, total enrichment takes ~12–16 s — well within the 20/min rate limit and the loading skeleton covers the wait.

Change: `candidates = mapped[:30]` → `candidates = mapped[:20]` (one line).

#### 3. Cache guard: skip caching all-null results (`steam/routes/market.py`)

Before writing to `_movers_cache`, check that at least one item in the result has a non-null `priceDelta7d`. If all deltas are null (enrichment fully failed), skip the cache write so the next request retries enrichment immediately.

```python
has_deltas = any(
    item.get("priceDelta7d") is not None
    for item in result["hot"] + result["cold"]
)
if has_deltas:
    _movers_cache[cache_key] = (result, now)
```

---

## What does NOT change

- `concurrency=5` in `_enrich_prices` — adequate for 20 items within 20/min.
- Sort logic — works correctly once deltas are non-null.
- Frontend — existing skeleton + TanStack Query `staleTime: 5 min` covers the enrichment wait time with no changes needed.
- Fallback path (topmovers from market-index) — untouched.
- All other TTLs (`INVENTORY_CACHE_TTL`, `ITEM_HISTORY_CACHE_TTL`, etc.) — untouched.

---

## Files changed

| File | Change |
|------|--------|
| `stores.py` | `MOVERS_CACHE_TTL = 82800` → `900` |
| `steam/routes/market.py` | candidates cap `30` → `20`; add cache guard |

---

## Success criteria

- Hot movers show real percentage deltas (not "N/A") after the initial skeleton load.
- Movers refresh every 15 minutes (not every 23 hours).
- No 429 rate-limit errors from steamwebapi for the movers enrichment path.
