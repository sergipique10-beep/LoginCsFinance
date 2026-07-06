# Inventory Manual Refresh Button — Design

**Date:** 2026-07-06
**Repos affected:** `LoginCsfinance` (backend), `CS-FINANCE-ionic` (frontend)

## Context

Steam inventory data is served from an in-memory backend cache with a 23h TTL
(`INVENTORY_CACHE_TTL`, `stores.py:36`), refreshed lazily on `GET /inventory`.
Users have no way to force a fresh fetch, and when refresh attempts fail
silently (steamwebapi/Steam errors), the frontend (TanStack Query) keeps
showing the last successful data with no error indicator
(`inventory.html`, `inventory.ts`). This design adds a manual refresh
button that forces a real re-fetch, while protecting the shared
steamwebapi.com quota (Starter plan: 20 req/min/endpoint, 2000/day,
10000/month) from abuse.

## Backend (`LoginCsfinance`)

- New forced-refresh path: `POST /inventory/refresh` (or `force=true` query
  param on the existing `GET /inventory`).
- New in-memory store in `stores.py`:
  `_inventory_refresh_cooldown: dict[str, float]` keyed by `steam_id`,
  cooldown window = 3600s (1h).
- Request handling:
  1. If `steam_id` has a cooldown timestamp less than 1h old → respond
     `429` with the remaining seconds, **without calling steamwebapi**.
  2. Otherwise → bypass `_inventory_cache`, call steamwebapi exactly as the
     normal flow does, then update both `_inventory_cache[steam_id]` and
     `_inventory_refresh_cooldown[steam_id] = now` on success.
- Reuse the existing error handling from `items.py:74-98` (403/429/502/504
  from steamwebapi/Steam) unchanged.
- Cooldown state lives server-side so it cannot be bypassed by calling the
  API directly instead of going through the app.

## Frontend (`CS-FINANCE-ionic`)

- Refresh icon/button in the inventory page header.
- On tap: call the forced-refresh endpoint; on success, update the
  TanStack Query cache with the fresh data (e.g. `setQueryData` +
  invalidate, or refetch).
- Disabled state for 1h after a successful manual refresh, persisted
  (e.g. `localStorage` timestamp) so it survives app reloads. This is a
  UX convenience only — the backend is the real enforcement point.
- If the backend returns `429` (cooldown active): show a toast with the
  remaining time, and sync the button's disabled state to it.
- If the request fails for another reason (403/502/504/steamwebapi rate
  limit): show an error toast. This also fixes the existing gap where
  `inventoryQuery.isError()` is never surfaced in the UI.

## Testing

- Backend: cooldown test — first forced refresh succeeds, a second one
  within 1h returns 429, a third one after the window expires succeeds
  again.
- Frontend: button disabled/enabled state test, and toast-on-429 /
  toast-on-error tests.

## Out of scope

- No changes to the passive 23h cache TTL or its existing error handling.
- No Redis/shared-state migration for multi-worker deployments — the
  cooldown store has the same single-worker limitation already noted in
  `stores.py:1-8` for the other in-memory caches.
