# Inventory Manual Refresh Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user force a fresh Steam inventory fetch on demand, bypassing the 23h backend cache, while a server-enforced 1h-per-user cooldown protects the shared steamwebapi.com quota from abuse.

**Architecture:** Backend gets a new `POST /inventory/refresh` endpoint that reuses the existing Steam-fetch/enrichment logic (extracted into a shared helper) but skips the 23h cache check and instead checks/updates a new in-memory cooldown store keyed by `steam_id`. The Angular frontend gets a button in the inventory page's filter row that calls this endpoint, writes the result straight into the TanStack Query cache, and persists a cooldown timestamp in `localStorage` to disable the button client-side (UX only — the backend is the real gate).

**Tech Stack:** FastAPI + httpx (backend, `LoginCsfinance`), Angular 20 + Ionic 8 + TanStack Angular Query (frontend, `CS-FINANCE-ionic`). Backend tests: pytest + FastAPI `TestClient` (new — no test suite exists yet). Frontend tests: Jasmine/Karma via `ng test` (existing convention).

## Global Constraints

- Cooldown window is exactly 3600 seconds (1h), enforced server-side per `steam_id`. (From the approved design spec, `docs/superpowers/specs/2026-07-06-inventory-manual-refresh-design.md`.)
- The forced refresh must bypass `_inventory_cache`'s 23h TTL but still update `_inventory_cache` on success, so the next passive `GET /inventory` within 23h serves the freshly-fetched data.
- Reuse the existing steamwebapi/Steam error handling (403/410/411/429/timeout/network/non-200) unchanged — do not alter `GET /inventory`'s behavior or response shape.
- No Redis/shared-state migration — the new cooldown store follows the same single-worker in-memory pattern already documented as a known limitation in `stores.py:1-8`.
- No toast/notification library exists in this codebase yet — do not introduce one; use inline UI state (text/icon), consistent with how the rest of the inventory page surfaces state (skeletons, plain text).

---

### Task 1: Backend — cooldown store, refactor, and `POST /inventory/refresh`

**Files:**
- Modify: `LoginCsfinance/stores.py:36-61` (add cooldown constant + store)
- Modify: `LoginCsfinance/steam/routes/items.py:8-11,65-110` (extract fetch helper, add endpoint)
- Modify: `LoginCsfinance/requirements.txt` (add `pytest`)
- Create: `LoginCsfinance/tests/__init__.py` (empty, makes `tests` a package)
- Create: `LoginCsfinance/tests/conftest.py`
- Create: `LoginCsfinance/tests/test_inventory_refresh.py`

**Interfaces:**
- Produces: `stores.INVENTORY_REFRESH_COOLDOWN: int = 3600`, `stores._inventory_refresh_cooldown: dict[str, float]`
- Produces: `steam.routes.items._fetch_fresh_inventory(request: Request, steam_id: str) -> list` — performs the Steam fetch + `_map_item` + `_enrich_market_prices` + `_enrich_images_from_cache` pipeline, raising the same `HTTPException`s the old `get_inventory` raised. Does NOT touch `_inventory_cache` or the cooldown store itself — callers do that.
- Produces: `POST /inventory/refresh` route, returns `list[dict]` (same shape as `GET /inventory`) on success, `429` with `detail` containing the remaining seconds when the cooldown is active.

- [ ] **Step 1: Add the cooldown constant and store to `stores.py`**

Edit `LoginCsfinance/stores.py`. Add the constant near the other cache TTLs (after line 46) and the store near the other cache stores (after line 61):

```python
INVENTORY_REFRESH_COOLDOWN = 3600  # 1h — manual "force refresh" button, protects shared steamwebapi quota
```

```python
_inventory_refresh_cooldown: dict[str, float] = {}  # steam_id → monotonic timestamp of last forced refresh
```

- [ ] **Step 2: Add pytest to `requirements.txt` and install it**

Append to `LoginCsfinance/requirements.txt`:

```
pytest==8.3.5
```

Run: `cd LoginCsfinance && venv/Scripts/python -m pip install pytest==8.3.5`
Expected: `Successfully installed pytest-8.3.5 ...` (plus its own deps: iniconfig, pluggy, etc.)

- [ ] **Step 3: Create the empty `tests` package**

Create `LoginCsfinance/tests/__init__.py` with empty content.

- [ ] **Step 4: Create `conftest.py` with a `client` fixture**

Create `LoginCsfinance/tests/conftest.py`:

```python
import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

import main
from main import app
from auth.service import require_jwt
from stores import _inventory_cache, _inventory_refresh_cooldown

STEAM_ID = "test_steam_id"


@pytest.fixture
def client(monkeypatch):
    # Skip the real ByMykel static-image fetch that main.py's lifespan performs on startup.
    monkeypatch.setattr(main, "_fetch_static_images", AsyncMock())

    app.dependency_overrides[require_jwt] = lambda: {"sub": STEAM_ID, "type": "access"}
    _inventory_cache.clear()
    _inventory_refresh_cooldown.clear()

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
```

- [ ] **Step 5: Write the failing tests for the refactor + new endpoint**

Create `LoginCsfinance/tests/test_inventory_refresh.py`:

```python
from unittest.mock import AsyncMock

from stores import INVENTORY_REFRESH_COOLDOWN, _inventory_cache
from steam.routes import items as items_routes
from tests.conftest import STEAM_ID

FRESH_ITEMS = [{"name": "AK-47 | Redline"}]


def _patch_fetch(monkeypatch, items=FRESH_ITEMS):
    mock = AsyncMock(return_value=items)
    monkeypatch.setattr(items_routes, "_fetch_fresh_inventory", mock)
    return mock


def _freeze_time(monkeypatch, start=1000.0):
    fake_now = [start]
    monkeypatch.setattr(items_routes.time, "monotonic", lambda: fake_now[0])
    return fake_now


def test_refresh_success_returns_fresh_items_and_updates_cache(client, monkeypatch):
    fake_now = _freeze_time(monkeypatch)
    _patch_fetch(monkeypatch)

    resp = client.post("/inventory/refresh")

    assert resp.status_code == 200
    assert resp.json() == FRESH_ITEMS
    assert _inventory_cache[STEAM_ID] == (FRESH_ITEMS, fake_now[0])


def test_refresh_bypasses_fresh_23h_cache(client, monkeypatch):
    fake_now = _freeze_time(monkeypatch)
    _inventory_cache[STEAM_ID] = ([{"name": "stale item"}], fake_now[0])
    _patch_fetch(monkeypatch)

    resp = client.post("/inventory/refresh")

    assert resp.status_code == 200
    assert resp.json() == FRESH_ITEMS


def test_second_refresh_within_cooldown_returns_429(client, monkeypatch):
    _freeze_time(monkeypatch)
    _patch_fetch(monkeypatch)

    first = client.post("/inventory/refresh")
    second = client.post("/inventory/refresh")

    assert first.status_code == 200
    assert second.status_code == 429
    assert "retry" in second.json()["detail"].lower()


def test_refresh_allowed_again_after_cooldown_expires(client, monkeypatch):
    fake_now = _freeze_time(monkeypatch)
    _patch_fetch(monkeypatch)

    first = client.post("/inventory/refresh")
    fake_now[0] += INVENTORY_REFRESH_COOLDOWN + 1
    second = client.post("/inventory/refresh")

    assert first.status_code == 200
    assert second.status_code == 200


def test_get_inventory_still_uses_23h_cache_unaffected_by_refresh_endpoint(client, monkeypatch):
    fake_now = _freeze_time(monkeypatch)
    _patch_fetch(monkeypatch)
    fetch_mock = items_routes._fetch_fresh_inventory

    first = client.get("/inventory")
    second = client.get("/inventory")

    assert first.status_code == 200
    assert second.status_code == 200
    assert fetch_mock.await_count == 1  # second GET served from cache, no re-fetch
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `cd LoginCsfinance && venv/Scripts/python -m pytest tests/test_inventory_refresh.py -v`
Expected: FAIL — `AttributeError` or `ImportError` on `_fetch_fresh_inventory` (doesn't exist yet) and `404 Not Found` on `POST /inventory/refresh` (route doesn't exist yet).

- [ ] **Step 7: Refactor `items.py` — extract `_fetch_fresh_inventory` and add the endpoint**

Edit `LoginCsfinance/steam/routes/items.py`. Update the `stores` import (line 8-11) to include the new names:

```python
from stores import (
    PROFILE_CACHE_TTL, INVENTORY_CACHE_TTL, ITEM_HISTORY_CACHE_TTL,
    INVENTORY_REFRESH_COOLDOWN,
    _profile_cache, _inventory_cache, _item_history_cache,
    _inventory_refresh_cooldown,
)
```

Replace the entire `get_inventory` function (lines 65-110) with:

```python
async def _fetch_fresh_inventory(request: Request, steam_id: str) -> list:
    try:
        resp = await request.app.state.http_client.get(
            f"{STEAM_WEB_API}/inventory",
            params={
                "steam_id": steam_id,
                "game": STEAM_GAME,
                "key": STEAM_API_KEY,
                "language": "english",
                "limit": 5000,
            },
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Steam inventory request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Steam: {exc}")

    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="Inventory is private")
    if resp.status_code in (410, 411):
        return []
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Steam rate limit — retry later")
    if resp.status_code != 200:
        logger.error("steamwebapi /inventory → %s: %.500s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail=f"Steam returned {resp.status_code}")

    data = resp.json()
    if not isinstance(data, list):
        logger.error("steamwebapi /inventory unexpected format: %.500s", resp.text)
        raise HTTPException(status_code=502, detail="Unexpected response format from Steam API")

    items = [_map_item(item) for item in data]
    items = await _enrich_market_prices(request.app.state.http_client, items)
    _enrich_images_from_cache(items)
    return items


@router.get("/inventory", summary="Inventario CS2 del usuario autenticado")
async def get_inventory(request: Request, user: dict = Depends(require_jwt)):
    steam_id: str = user["sub"]

    now = time.monotonic()
    cached = _inventory_cache.get(steam_id)
    if cached and now - cached[1] < INVENTORY_CACHE_TTL:
        return cached[0]

    items = await _fetch_fresh_inventory(request, steam_id)
    _inventory_cache[steam_id] = (items, now)
    return items


@router.post("/inventory/refresh", summary="Fuerza un refresh del inventario ignorando el caché de 23h")
async def refresh_inventory(request: Request, user: dict = Depends(require_jwt)):
    steam_id: str = user["sub"]

    now = time.monotonic()
    cooldown_start = _inventory_refresh_cooldown.get(steam_id)
    if cooldown_start and now - cooldown_start < INVENTORY_REFRESH_COOLDOWN:
        remaining = int(INVENTORY_REFRESH_COOLDOWN - (now - cooldown_start))
        raise HTTPException(status_code=429, detail=f"Refresh cooldown active — retry in {remaining}s")

    items = await _fetch_fresh_inventory(request, steam_id)
    _inventory_cache[steam_id] = (items, now)
    _inventory_refresh_cooldown[steam_id] = now
    return items
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `cd LoginCsfinance && venv/Scripts/python -m pytest tests/test_inventory_refresh.py -v`
Expected: `5 passed`

- [ ] **Step 9: Commit**

```bash
cd "LoginCsfinance"
git add stores.py steam/routes/items.py requirements.txt tests/__init__.py tests/conftest.py tests/test_inventory_refresh.py
git commit -m "$(cat <<'EOF'
Add POST /inventory/refresh with server-enforced 1h cooldown

Extracts the Steam fetch/enrichment pipeline into _fetch_fresh_inventory
so both the passive 23h-cached GET /inventory and the new forced-refresh
endpoint share it. The cooldown is tracked server-side per steam_id so it
can't be bypassed by calling the API directly.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Frontend — `InventoryService.refreshInventory()`

**Files:**
- Modify: `CS-FINANCE-ionic/src/app/features/inventory/services/inventory.service.ts`
- Modify: `CS-FINANCE-ionic/src/app/features/inventory/services/inventory.service.spec.ts`

**Interfaces:**
- Consumes: `SteamApiService.post<T>(path: string, body: unknown): Observable<T>` (existing, `steam-api.service.ts:21-23`)
- Produces: `InventoryService.refreshInventory(): Observable<ISkinCard[]>` — POSTs to `inventory/refresh`, used by Task 3's component.

- [ ] **Step 1: Write the failing test**

Edit `CS-FINANCE-ionic/src/app/features/inventory/services/inventory.service.spec.ts`, adding imports and a new test:

```typescript
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideAngularQuery, QueryClient } from '@tanstack/angular-query-experimental';

import { InventoryService } from './inventory.service';
import { environment } from '../../../../environments/environment';

describe('InventoryService', () => {
  let service: InventoryService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideAngularQuery(new QueryClient({ defaultOptions: { queries: { retry: false } } })),
      ],
    });
    service = TestBed.inject(InventoryService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should expose injectUserInventory as a method', () => {
    expect(typeof service.injectUserInventory).toBe('function');
  });

  it('refreshInventory: hace POST a inventory/refresh y devuelve los items', () => {
    const items = [{ id: 'a', name: 'AK-47 | Redline' }];

    service.refreshInventory().subscribe(result => {
      expect(result).toEqual(items as never);
    });

    const req = httpMock.expectOne(`${environment.apiUrl}/inventory/refresh`);
    expect(req.request.method).toBe('POST');
    req.flush(items);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd CS-FINANCE-ionic && npx ng test --include='**/inventory.service.spec.ts' --watch=false`
Expected: FAIL — `TypeError: service.refreshInventory is not a function`

- [ ] **Step 3: Add `refreshInventory()` to `InventoryService`**

Edit `CS-FINANCE-ionic/src/app/features/inventory/services/inventory.service.ts`, adding this method after `injectItemHistory` (after line 39):

```typescript
  refreshInventory() {
    return this.api.post<ISkinCard[]>('inventory/refresh', {});
  }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd CS-FINANCE-ionic && npx ng test --include='**/inventory.service.spec.ts' --watch=false`
Expected: `3 specs, 0 failures`

- [ ] **Step 5: Commit**

```bash
cd "CS-FINANCE-ionic"
git add src/app/features/inventory/services/inventory.service.ts src/app/features/inventory/services/inventory.service.spec.ts
git commit -m "$(cat <<'EOF'
Add InventoryService.refreshInventory() for manual inventory refresh

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Frontend — refresh button, cooldown UX, and error display

**Files:**
- Modify: `CS-FINANCE-ionic/src/app/features/inventory/pages/inventory.ts`
- Modify: `CS-FINANCE-ionic/src/app/features/inventory/pages/inventory.html`
- Modify: `CS-FINANCE-ionic/src/app/features/inventory/pages/inventory.scss`
- Create: `CS-FINANCE-ionic/src/app/features/inventory/pages/inventory.spec.ts`

**Interfaces:**
- Consumes: `InventoryService.refreshInventory(): Observable<ISkinCard[]>` (Task 2), `QueryClient.setQueryData(queryKey, data)` (TanStack Angular Query, already provided app-wide in `main.ts:29-39`).
- Produces: `Inventory.onRefreshInventory(): void`, `Inventory.canRefresh: Signal<boolean>`, `Inventory.isRefreshing: Signal<boolean>`, `Inventory.refreshError: Signal<string | null>` — all read directly in the template, nothing else depends on them.

- [ ] **Step 1: Write the failing component tests**

Create `CS-FINANCE-ionic/src/app/features/inventory/pages/inventory.spec.ts`:

```typescript
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter, RouteReuseStrategy } from '@angular/router';
import { IonicRouteStrategy } from '@ionic/angular/standalone';
import { signal } from '@angular/core';
import { of, throwError } from 'rxjs';
import { HttpErrorResponse } from '@angular/common/http';
import { provideAngularQuery, QueryClient } from '@tanstack/angular-query-experimental';

import { Inventory } from './inventory';
import { InventoryService } from '../services/inventory.service';
import { PriceProviderService } from '../../../shared/services/price-provider.service';

function makeQueryStub(dataValue: unknown = null) {
  return {
    data: signal(dataValue),
    isLoading: signal(false),
    isPending: signal(false),
    isError: signal(false),
    isFetching: signal(false),
    isSuccess: signal(true),
    error: signal(null),
    status: signal('success'),
  };
}

describe('Inventory — refresh manual', () => {
  let component: Inventory;
  let fixture: ComponentFixture<Inventory>;
  let inventoryServiceMock: {
    injectUserInventory: jasmine.Spy;
    getPortfolioHistory: jasmine.Spy;
    savePortfolioSnapshot: jasmine.Spy;
    refreshInventory: jasmine.Spy;
  };

  beforeEach(async () => {
    localStorage.clear();

    inventoryServiceMock = {
      injectUserInventory: jasmine.createSpy().and.returnValue(makeQueryStub([])),
      getPortfolioHistory: jasmine.createSpy().and.returnValue([]),
      savePortfolioSnapshot: jasmine.createSpy(),
      refreshInventory: jasmine.createSpy(),
    };

    const priceProviderServiceMock = { selectedMarket: signal('steam') };

    await TestBed.configureTestingModule({
      imports: [Inventory],
      providers: [
        { provide: RouteReuseStrategy, useClass: IonicRouteStrategy },
        { provide: InventoryService, useValue: inventoryServiceMock },
        { provide: PriceProviderService, useValue: priceProviderServiceMock },
        provideRouter([]),
        provideAngularQuery(new QueryClient({ defaultOptions: { queries: { retry: false } } })),
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(Inventory);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('canRefresh: true por defecto (sin cooldown previo)', () => {
    expect(component.canRefresh()).toBeTrue();
  });

  it('onRefreshInventory: en éxito, actualiza la query cache y activa el cooldown', () => {
    const fresh = [{ id: 'a', name: 'AK-47 | Redline' }];
    inventoryServiceMock.refreshInventory.and.returnValue(of(fresh as never));

    component.onRefreshInventory();

    expect(component.isRefreshing()).toBeFalse();
    expect(component.canRefresh()).toBeFalse();
    expect(component.refreshError()).toBeNull();
    expect(localStorage.getItem('inventory_refresh_cooldown_until')).not.toBeNull();
  });

  it('onRefreshInventory: en error 429, muestra el mensaje del backend y activa cooldown', () => {
    const err = new HttpErrorResponse({
      status: 429,
      error: { detail: 'Refresh cooldown active — retry in 1800s' },
    });
    inventoryServiceMock.refreshInventory.and.returnValue(throwError(() => err));

    component.onRefreshInventory();

    expect(component.isRefreshing()).toBeFalse();
    expect(component.refreshError()).toContain('cooldown');
    expect(component.canRefresh()).toBeFalse();
  });

  it('onRefreshInventory: en error distinto de 429, muestra error genérico sin activar cooldown', () => {
    const err = new HttpErrorResponse({ status: 502, error: { detail: 'Steam returned 502' } });
    inventoryServiceMock.refreshInventory.and.returnValue(throwError(() => err));

    component.onRefreshInventory();

    expect(component.isRefreshing()).toBeFalse();
    expect(component.refreshError()).not.toBeNull();
    expect(component.canRefresh()).toBeTrue();
  });

  it('onRefreshInventory: no hace nada si canRefresh() es false', () => {
    localStorage.setItem('inventory_refresh_cooldown_until', String(Date.now() + 60_000));
    fixture = TestBed.createComponent(Inventory);
    component = fixture.componentInstance;
    fixture.detectChanges();

    component.onRefreshInventory();

    expect(inventoryServiceMock.refreshInventory).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd CS-FINANCE-ionic && npx ng test --include='**/inventory.spec.ts' --watch=false`
Expected: FAIL — compile error, `canRefresh`/`isRefreshing`/`refreshError`/`onRefreshInventory` don't exist on `Inventory` yet.

- [ ] **Step 3: Implement the cooldown/refresh logic in `inventory.ts`**

Edit `CS-FINANCE-ionic/src/app/features/inventory/pages/inventory.ts`.

Update the imports (replace lines 1-12):

```typescript
import { ChangeDetectionStrategy, Component, computed, effect, inject, OnInit, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { IonContent, IonButton, IonModal, IonSkeletonText, IonIcon } from '@ionic/angular/standalone';
import { addIcons } from 'ionicons';
import { arrowUpOutline, arrowDownOutline, optionsOutline, refreshOutline } from 'ionicons/icons';
import { QueryClient } from '@tanstack/angular-query-experimental';

import { InventoryService } from '../services/inventory.service';
import { ISkinCard, IPortfolioChartPoint, IPortfolioStats } from '../../../models/interfaces';
import { Search } from '../../../shared/components/search/search';
import { SkinCard } from '../../../shared/components/skin-card/skin-card';
import { PriceProviderService } from '../../../shared/services/price-provider.service';
import { PriceProviderSelector } from '../../../shared/components/price-provider-selector/price-provider-selector';
import { SkinDetailSheetComponent } from '../../../shared/components/skin-detail-sheet/skin-detail-sheet.component';
```

Update the `@Component` decorator's `imports` array (line 22) to add `IonIcon`:

```typescript
  imports: [IonContent, IonButton, IonModal, IonSkeletonText, IonIcon, Search, SkinCard, PriceProviderSelector, SkinDetailSheetComponent],
```

Add a module-level constant right after the `type SortOrder = 'desc' | 'asc';` line (line 14):

```typescript
const REFRESH_COOLDOWN_STORAGE_KEY = 'inventory_refresh_cooldown_until';
```

Inside the `Inventory` class, add the `QueryClient` injection alongside the other `inject()` calls (after line 26):

```typescript
  private readonly queryClient = inject(QueryClient);
```

Add the new signals right after `readonly inventoryQuery = ...` (after line 29):

```typescript
  readonly isRefreshing = signal(false);
  readonly refreshError = signal<string | null>(null);
  readonly refreshCooldownUntil = signal<number>(this.readStoredCooldownUntil());

  readonly canRefresh = computed(() => Date.now() >= this.refreshCooldownUntil());
```

Add the new methods at the end of the class, right before the closing `}` (after line 155, i.e. after `healthColor`):

```typescript

  private readStoredCooldownUntil(): number {
    const raw = localStorage.getItem(REFRESH_COOLDOWN_STORAGE_KEY);
    const parsed = raw ? Number(raw) : 0;
    return Number.isFinite(parsed) ? parsed : 0;
  }

  private setCooldown(durationMs: number): void {
    const until = Date.now() + durationMs;
    this.refreshCooldownUntil.set(until);
    localStorage.setItem(REFRESH_COOLDOWN_STORAGE_KEY, String(until));
  }

  onRefreshInventory(): void {
    if (!this.canRefresh() || this.isRefreshing()) return;

    this.isRefreshing.set(true);
    this.refreshError.set(null);

    this.inventoryService.refreshInventory().subscribe({
      next: (items: ISkinCard[]) => {
        this.queryClient.setQueryData(['inventory'], items);
        this.setCooldown(60 * 60 * 1000);
        this.isRefreshing.set(false);
      },
      error: (err: HttpErrorResponse) => {
        const detail = (err.error?.detail as string) ?? 'No se pudo refrescar el inventario';
        this.refreshError.set(detail);
        if (err.status === 429) {
          this.setCooldown(60 * 60 * 1000);
        }
        this.isRefreshing.set(false);
      },
    });
  }
```

Update the `constructor()` (lines 81-87) to register the new icon:

```typescript
  constructor() {
    addIcons({ arrowUpOutline, arrowDownOutline, optionsOutline, refreshOutline });
    effect(() => {
      const skins = this.inventoryQuery.data();
      if (skins) this.refreshPortfolioFromSkins(skins);
    });
  }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd CS-FINANCE-ionic && npx ng test --include='**/inventory.spec.ts' --watch=false`
Expected: `5 specs, 0 failures`

- [ ] **Step 5: Add the button to the template**

Edit `CS-FINANCE-ionic/src/app/features/inventory/pages/inventory.html`. Replace the `filter-row` block (lines 109-118) with:

```html
    <!-- ── Filters ── -->
    <div class="filter-row">
      <ion-button class="filter-btn" fill="outline" size="small" (click)="toggleSort()">
        PRICE: {{ sortOrder() === 'desc' ? 'HIGH/LOW' : 'LOW/HIGH' }}
        <span class="sort-arrow">{{ sortOrder() === 'desc' ? ' ↓' : ' ↑' }}</span>
      </ion-button>
      <ion-button class="filter-btn item-type-btn" fill="outline" size="small">
        ITEM TYPE ▾
      </ion-button>
      <ion-button
        class="filter-btn refresh-btn"
        fill="outline"
        size="small"
        [disabled]="!canRefresh() || isRefreshing()"
        (click)="onRefreshInventory()"
      >
        <ion-icon slot="icon-only" name="refresh-outline" [class.spinning]="isRefreshing()" />
      </ion-button>
    </div>

    @if (refreshError(); as error) {
      <div class="refresh-error">{{ error }}</div>
    }
```

- [ ] **Step 6: Add minimal styles**

Edit `CS-FINANCE-ionic/src/app/features/inventory/pages/inventory.scss`. Add after the existing `.filter-btn` block (after line 184):

```scss
.refresh-btn {
  flex: 0 0 auto;
  --padding-start: 8px;
  --padding-end: 8px;

  .spinning {
    animation: inventory-refresh-spin 0.8s linear infinite;
  }
}

@keyframes inventory-refresh-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.refresh-error {
  font-size: 11px;
  color: #ff6b6b;
  margin: -6px 2px 12px;
}
```

- [ ] **Step 7: Manually verify in the browser**

Run: `cd CS-FINANCE-ionic && npm start`
Open the app, log in, go to the inventory tab, and confirm:
- The refresh icon appears next to the PRICE/ITEM TYPE filter buttons.
- Clicking it disables the button and (once the backend responds) the inventory list updates.
- Clicking it again immediately shows the cooldown error (backend 429) and keeps the button disabled.
- Reloading the page keeps the button disabled (cooldown persisted in `localStorage`).

- [ ] **Step 8: Commit**

```bash
cd "CS-FINANCE-ionic"
git add src/app/features/inventory/pages/inventory.ts src/app/features/inventory/pages/inventory.html src/app/features/inventory/pages/inventory.scss src/app/features/inventory/pages/inventory.spec.ts
git commit -m "$(cat <<'EOF'
Add manual inventory refresh button with client-side cooldown UX

Calls the new POST /inventory/refresh endpoint, writes the result
straight into the TanStack Query cache, and disables the button for 1h
(persisted in localStorage) to mirror the backend-enforced cooldown.
Also surfaces refresh errors inline, which previously failed silently.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
