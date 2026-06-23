# Movers Enrichment Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `priceDelta7d: null` on hot movers by recalibrating the cache TTL and candidate count for the Starter plan.

**Architecture:** Two targeted edits to existing files — reduce `MOVERS_CACHE_TTL` from 23 h to 15 min so broken enrichments don't persist, shrink the candidate pool from 30 to 20 (matching the exact number of items displayed), and add a guard that skips caching when all deltas are null.

**Tech Stack:** Python 3.11, FastAPI, httpx, in-memory dict cache (`stores.py`).

## Global Constraints

- No automated test suite — verification is done by inspecting API responses directly.
- Do not change `concurrency` in `_enrich_prices` (stays at 5).
- Do not touch any TTL other than `MOVERS_CACHE_TTL`.
- Do not change the fallback path (topmovers from market-index).
- Server start command: `uvicorn main:app --host 127.0.0.1 --port 8000 --reload` from `LoginCsfinance/`.

---

### Task 1: Reduce MOVERS_CACHE_TTL

**Files:**
- Modify: `stores.py:39`

**Interfaces:**
- Produces: `MOVERS_CACHE_TTL = 900` (imported by `steam/routes/market.py`)

- [ ] **Step 1: Edit `stores.py`**

Change line 39 from:
```python
MOVERS_CACHE_TTL = 82800     # 23 h — free plan: 5 req/day, must match other caches
```
to:
```python
MOVERS_CACHE_TTL = 900       # 15 min — Starter plan: 2,000 req/day
```

- [ ] **Step 2: Verify the server still starts**

```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```
Expected: no import errors, server listening on port 8000.

- [ ] **Step 3: Commit**

```bash
git add stores.py
git commit -m "fix: reduce MOVERS_CACHE_TTL to 15 min for Starter plan"
```

---

### Task 2: Reduce candidates cap and add cache guard

**Files:**
- Modify: `steam/routes/market.py:100-118`

**Interfaces:**
- Consumes: `MOVERS_CACHE_TTL = 900` from Task 1.
- Consumes: `_enrich_prices(client, candidates)` → returns list of items, each with `priceDelta7d: float | None`.
- Produces: `_movers_cache[cache_key]` written only when `has_deltas` is True.

- [ ] **Step 1: Update the comment and cap on line 100-103**

Replace:
```python
            # Sort by price descending — higher-priced items tend to have more price movement.
            # Cap at 30 to avoid exhausting /history rate limits on first load.
            # _fetch_history_for_item caches results for 23h so subsequent calls are free.
            mapped.sort(key=lambda x: x["priceLatest"], reverse=True)
            candidates = mapped[:30]
```
with:
```python
            # Sort by price descending — higher-priced items tend to have more price movement.
            # Cap at 20 (= _MOVERS_LIMIT * 2): exactly the number of items displayed,
            # and safe within the Starter plan's 20 req/min rate limit.
            mapped.sort(key=lambda x: x["priceLatest"], reverse=True)
            candidates = mapped[:_MOVERS_LIMIT * 2]
```

- [ ] **Step 2: Add cache guard on line 118**

Replace:
```python
            _movers_cache[cache_key] = (result, now)
            return result
```
with:
```python
            has_deltas = any(
                item.get("priceDelta7d") is not None
                for item in result["hot"] + result["cold"]
            )
            if has_deltas:
                _movers_cache[cache_key] = (result, now)
            return result
```

- [ ] **Step 3: Verify the server still starts**

```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```
Expected: no errors.

- [ ] **Step 4: Verify the endpoint returns non-null deltas**

```bash
# Requires a valid JWT. Set TOKEN to your current access token.
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/market/movers \
  | python -m json.tool \
  | grep priceDelta7d
```
Expected: at least several lines with numeric values (not `null`) in the `hot` block.

- [ ] **Step 5: Commit**

```bash
git add steam/routes/market.py
git commit -m "fix: cap movers candidates at 20 and skip cache on all-null deltas"
```
