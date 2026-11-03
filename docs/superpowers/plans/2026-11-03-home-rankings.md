# Rankings de Home — plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sustituir HOT/COLD de Home por tres listas —cajas más vendidas, skins que más suben y que más bajan en dólares a 30 días— calculadas sobre todo el mercado, incluidas las skins caras.

**Architecture:** El movers-tick (`POST /internal/movers-tick`, cada 15 min) pide `/items` dos veces (las 5.000 más vendidas y las de precio real ≥ 50 $), calcula las tres listas con funciones puras nuevas (`steam/domain/home_rankings.py`) y las guarda en `market_movers` con `bucket` = `cases` | `up` | `down`. `GET /market/movers` devuelve `{cases, up, down}`. El front pinta tres bloques en Home y el ticker usa `up`/`down`.

**Tech Stack:** FastAPI + supabase-py + pytest (back, `LoginCsfinance`); Angular 20 standalone + signals + TanStack Query + ngx-translate + Karma (front, `CS-FINANCE-ionic`).

**Spec:** `docs/superpowers/specs/2026-11-03-home-rankings-design.md` (repo del back).

## Global Constraints

- Rama `feat/home-rankings` en los dos repos, creada encima de `feat/ux46-data-status`. Commit + push sí; merge a `master`/`main` nunca (lo hace el usuario).
- Precio: `pricereal` para los deltas y `pricemedian` como `priceLatest`. **Nunca `pricelatestsell`** en estas listas.
- Periodo de suben/bajan: **30 días** (`pricereal` frente a `pricereal30d`).
- Mínimo de ventas para suben/bajan: **`sold7d >= 5`**.
- Plausibilidad: se descarta la skin si `rules.plausible_ratio(pricereal, pricereal30d)` es falso (10×).
- Cajas: nombre termina en `" Case"`; sin mínimo de precio; orden por `sold24h` descendente; `sold24h > 0`.
- "Skin" para suben/bajan = todo lo que no es caja (agentes, stickers, charms incluidos). Sin tope por tipo.
- **Sin relleno**: suben solo con cambio > 0, bajan solo con cambio < 0. Nunca 0 %.
- **10** items por lista (`HOME_RANKING_LIMIT = 10`).
- Petición de caras: `sort_by=soldZa`, `price_real_min=50`, `max=5000`.
- Contrato de tests CAL-09: los tests de claves exactas se cambian **a propósito**, no se borran.
- i18n: claves nuevas en `es.json` y `en.json` a la vez (paridad).

## Review Focus

1. **Falla solo la petición de las caras** → lo demás se guarda y la API dice `partial`, no `error` ni lista vacía. Test en Task 2 (`test_compute_caras_fallan_es_partial`).
2. **La misma skin viene en las dos respuestas** → aparece una sola vez. Test en Task 1 (`test_universo_sin_duplicados`).
3. **`pricereal30d` ausente, 0 o a más de 10× del actual** → la skin no entra (ni como 0 %). Test en Task 1 (`test_cambio_30d_descarta_basura`).
4. **Snapshot viejo con buckets `hot`/`cold` justo tras el deploy** (antes del primer tick) → la API responde `{cases: [], up: [], down: []}` sin romper. Test en Task 3 (`test_movers_ignora_buckets_viejos`).
5. **Divisa EUR y cambios negativos** → `formatUsdChange(-208.33, EUR)` da `-€…` convertido, y `null` da `N/A`. Test en Task 6.

---

### Task 0: Ramas

**Files:** ninguno (git).

- [ ] **Step 1: Back.** En `LoginCsfinance` (árbol principal, ahora en `feat/ux46-data-status`):
  - quitar el worktree `wt-home-rankings` del scratchpad (`git worktree remove`), tras comprobar que no tiene cambios sin commitear;
  - borrar la copia sin seguimiento `docs/superpowers/specs/2026-11-03-home-rankings-design.md` del árbol principal (idéntica a la commiteada);
  - `git switch feat/home-rankings` y `git merge --no-edit feat/ux46-data-status`.
  Expected: merge limpio; `git log --oneline -3` muestra el merge sobre el commit de docs.
- [ ] **Step 2: Front.** En `CS-FINANCE-ionic`: `git switch -c feat/home-rankings feat/ux46-data-status`.
- [ ] **Step 3:** `git push -u origin feat/home-rankings` en los dos.

---

### Task 1: Reglas puras de los rankings (back)

**Files:**
- Create: `steam/domain/home_rankings.py`
- Create: `tests/test_home_rankings.py`
- Modify: `docs/features/steam.md` (frontmatter `files:` + párrafo corto; el ratchet exige documentar ficheros nuevos)

**Interfaces:**
- Consumes: `SteamItem` (`steam/domain/models.py`), `rules.plausible_ratio`.
- Produces:
  - `HOME_RANKING_LIMIT: int = 10`, `MIN_SOLD_7D: int = 5`, `PRICE_REAL_MIN_EXPENSIVE: float = 50`
  - `is_case(item: SteamItem) -> bool`
  - `merge_universe(*batches: list[SteamItem]) -> list[SteamItem]` — dedupe por `item.name`, gana la primera aparición.
  - `change_30d(item: SteamItem) -> tuple[float, float] | None` — `(usd, pct)` sin redondear, o `None`.
  - `top_cases(items: list[SteamItem]) -> list[SteamItem]`
  - `top_movers(items: list[SteamItem], direction: Literal["up", "down"]) -> list[tuple[SteamItem, float, float]]` — `(item, usd, pct)`.
  - `usd_from_pct(price_now: float | None, pct: float | None) -> float | None` — `price_now - price_now / (1 + pct/100)`; `None` si falta algo o `pct == -100`.

- [ ] **Step 1: Escribir los tests** (helper `_item(name, real=…, real30=…, sold7=…, sold24=…)` que construye un `SteamItem` con el resto de campos a `None`/`()`):

```python
def test_is_case():
    assert is_case(_item("Dreams & Nightmares Case"))
    assert not is_case(_item("Sticker Capsule"))
    assert not is_case(_item("AK-47 | Case Hardened (Field-Tested)"))

def test_universo_sin_duplicados():
    a, b = _item("X", real=10), _item("X", real=99)
    assert [i.price_real for i in merge_universe([a], [b, _item("Y")])] == [10, None]

def test_cambio_30d():
    usd, pct = change_30d(_item("K", real=1072.82, real30=1175.0))
    assert round(usd, 2) == -102.18 and round(pct, 1) == -8.7

@pytest.mark.parametrize("real, real30", [(100, None), (100, 0), (None, 50), (148.35, 1.83)])
def test_cambio_30d_descarta_basura(real, real30):
    assert change_30d(_item("G", real=real, real30=real30)) is None

def test_top_cases_por_unidades_sin_ceros():
    items = [_item("A Case", sold24=5), _item("B Case", sold24=110497), _item("C Case", sold24=0), _item("AK | X", sold24=9999)]
    assert [i.name for i in top_cases(items)] == ["B Case", "A Case"]

def test_top_movers_dolares_sin_relleno_y_minimo_ventas():
    items = [
        _item("Knife", real=750.95, real30=863.06, sold7=10),   # -112.11
        _item("Gloves", real=96.80, real30=62.64, sold7=11),    # +34.16
        _item("Flat", real=5, real30=5, sold7=50),              # 0 → fuera
        _item("Thin", real=600, real30=300, sold7=4),           # < 5 ventas → fuera
        _item("Big Case", real=10, real30=5, sold7=99),         # caja → fuera
    ]
    assert [i.name for i, *_ in top_movers(items, "up")] == ["Gloves"]
    assert [i.name for i, *_ in top_movers(items, "down")] == ["Knife"]

def test_top_movers_limite():
    items = [_item(f"S{n}", real=100 + n, real30=100, sold7=5) for n in range(1, 15)]
    assert len(top_movers(items, "up")) == HOME_RANKING_LIMIT
    assert top_movers(items, "up")[0][0].name == "S14"

def test_usd_from_pct():
    _, pct = change_30d(_item("G", real=96.80, real30=62.64))
    assert round(usd_from_pct(96.80, pct), 2) == 34.16
    assert usd_from_pct(None, 5) is None and usd_from_pct(10, None) is None
```

- [ ] **Step 2:** `venv/Scripts/python.exe -m pytest tests/test_home_rankings.py -q` → FAIL (módulo no existe).
- [ ] **Step 3: Implementar** `steam/domain/home_rankings.py` con las firmas de arriba. `top_movers` filtra `not is_case`, `sold_7d >= MIN_SOLD_7D`, `change_30d` no `None`, signo estricto según `direction`; ordena por `usd` (desc para `up`, asc para `down`); corta a `HOME_RANKING_LIMIT`. Docstring del módulo: por qué existen las reglas (venta suelta, basura de `pricereal30d`, sin relleno), en el estilo de `rules.py`.
- [ ] **Step 4:** el mismo pytest → PASS.
- [ ] **Step 5:** añadir `steam/domain/home_rankings.py` al `files:` de `docs/features/steam.md`. Commit `feat(rankings): reglas puras de cajas y suben/bajan en dólares (home-rankings)`.

---

### Task 2: Cálculo en el tick (back)

**Files:**
- Modify: `steam/api/steam_client.py:85-95` (`items`)
- Modify: `steam/services/rankings_service.py` (`_MOVERS_SELECT`, `compute_movers` → `compute_home_rankings`, `capture_movers`)
- Modify: `tests/test_market_service.py` (tests de rankings), `tests/test_movers_tick_vacio.py`, `tests/test_steam_client.py`
- Test: `tests/test_home_rankings.py` (casos de servicio, con `steam_api`/`http` como en `test_market_service.py`)

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `steam_client.items(..., price_real_min: float | None = None)` → añade `params["price_real_min"]` solo si no es `None`.
  - `rankings_service.compute_home_rankings(client) -> Fetched[dict[str, list[MoverCard]]]` con claves exactas `cases`, `up`, `down`. Sustituye a `compute_movers` (se borra, junto a `_build_movers_from_topmovers` si ya nadie la usa; `compute_trending` sigue usando `_topmovers`).
  - `MoverCard(SkinCard)` en `steam/domain/models.py` con `priceChange30d: float | None`.
  - Cada tarjeta = `_map_item(item)` con: `priceLatest = item.price_median`, `priceDelta30d = pct` sin redondear (solo en `up`/`down`), `priceChange30d = usd` (en `cases`: `None`).

- [ ] **Step 1: Tests de servicio** (fixture de `/items` con dos respuestas: registrar `steam_api.on("api/items", ...)` distinguiendo por el parámetro `price_real_min` — mira cómo el fake de `steam_api` casa rutas y, si no admite params, añade el emparejamiento por query en el fake con su propio test):
  - `test_compute_dos_peticiones_y_tres_listas`: la barata trae `"Dreams & Nightmares Case"` (sold24h 110497) y una skin que sube; la cara trae un cuchillo que baja y repite la skin que sube → `set(data) == {"cases","up","down"}`, sin duplicados, `status == "ok"`.
  - `test_compute_usa_mediana_no_ultima_venta`: item con `pricelatestsell=342.12`, `pricemedian=0.03` → `priceLatest == 0.03`.
  - `test_compute_caras_fallan_es_partial`: la cara devuelve 500 → `status == "partial"`, `reason == "http_500"`, `cases` no vacío.
  - `test_compute_baratas_fallan_es_error`: la barata devuelve 500 → `Fetched({"cases": [], "up": [], "down": []}, "error", "http_500")`.
  - `test_steam_client`: `items(..., price_real_min=50)` manda `price_real_min=50` en la query; sin el argumento, no lo manda.
- [ ] **Step 2:** pytest de esos tests → FAIL.
- [ ] **Step 3: Implementar.** `_MOVERS_SELECT` añade `"pricemedian"`. `compute_home_rankings`: pide las dos con `_ranking_items` (o una variante que acepte `price_real_min`), une con `merge_universe(baratas, caras)`, aplica `top_cases`/`top_movers`, mapea a tarjetas, quita sticker slabs como hoy (`is_sticker_slab`), enriquece imágenes (`catalog_service`) y precios de mercado (`pricing_service.enrich_market_prices`) igual que el código actual. **No** llama a `pricing_service.enrich_prices` (CSFloat history). Si falla la barata → `degraded("movers", reason, "error", vacío)`; si falla solo la cara → `degraded("movers", reason, "partial", data)`.
- [ ] **Step 4:** `capture_movers` usa `compute_home_rankings` y guarda filas con `_to_row(card, rank, bucket)` para `bucket in ("cases", "up", "down")`. Se mantiene CAL-10: sin filas, no se reemplaza. Actualizar `test_movers_tick_vacio.py` y quitar `compute_movers` de los parametrize de `test_market_service.py` (trending se queda; el test de topmovers fresco/caducado pasa a usar `compute_trending`).
- [ ] **Step 5:** pytest de los ficheros tocados → PASS. Commit `feat(rankings): el movers-tick calcula cajas y suben/bajan con dos peticiones a /items (home-rankings)`.

---

### Task 3: Lectura y API (back)

**Files:**
- Modify: `steam/services/rankings_service.py` (`get_movers`)
- Modify: `steam/routes/market.py:25-30` (summary de la ruta)
- Modify: `tests/test_steam_contract_market.py` (`test_movers_y_trending_sirven_snapshots`), `tests/test_data_status_headers.py` (casos de movers)
- Test: `tests/test_home_rankings.py`

**Interfaces:**
- Consumes: `usd_from_pct` (Task 1), `MoverCard` (Task 2).
- Produces: `GET /market/movers` → `{"cases": [RankedCard], "up": [MoverCard], "down": [MoverCard]}`; `up`/`down` llevan `priceChange30d = usd_from_pct(priceReal, priceDelta30d)`. Cabeceras UX-46 sin cambios (`movers_snapshot`, `updated_at`, 45 min).

- [ ] **Step 1: Tests:**
  - contrato CAL-09: `set(movers) == {"cases", "up", "down"}`; `set(movers["cases"][0]) == ROW_ITEM_KEYS`; `set(movers["up"][0]) == ROW_ITEM_KEYS | {"priceChange30d"}`.
  - `test_movers_ignora_buckets_viejos`: filas con `bucket` `hot`/`cold` → `{"cases": [], "up": [], "down": []}`, 200.
  - `test_movers_cambio_en_dolares`: fila `up` con `price_real=96.80` y `price_delta_30d` = pct exacto → `priceChange30d` ≈ 34.16.
  - `test_data_status_headers.py`: los casos de movers usan buckets nuevos y siguen dando `stale`/`snapshot_age`.
- [ ] **Step 2:** FAIL. **Step 3:** implementar `get_movers` (agrupa por `bucket`, añade `priceChange30d` a `up`/`down`) y el summary: «Rankings de Home: cajas más vendidas y skins que más suben/bajan en $ a 30 días».
- [ ] **Step 4:** PASS. Commit `feat(rankings): /market/movers sirve cajas, suben y bajan (home-rankings)`.

---

### Task 4: Herramienta del chat (back)

**Files:**
- Modify: `tools/market_tools.py:222-231` (`_ver_movers`) y `:300-304` (descripción)
- Modify: `tests/test_steam_contract_tools.py:89-100`

- [ ] **Step 1:** test: `set(await _ver_movers(client=api)) == {"cases", "up", "down"}` con filas de esos buckets; `up[0]` incluye `priceChange30d`.
- [ ] **Step 2:** FAIL. **Step 3:** `_ver_movers` reutiliza `rankings_service.get_movers()` (no repite el agrupado) y pasa cada lista por `_para_llm`. Descripción: «Rankings del mercado CS2: cajas más vendidas (24h) y skins que más suben y más bajan en dólares en los últimos 30 días».
- [ ] **Step 4:** PASS. Commit `feat(chat): ver_movers devuelve los rankings nuevos (home-rankings)`.

---

### Task 5: Cierre del back

**Files:** `CLAUDE.md` (fila de `/market/movers`), `docs/features/steam.md` (párrafo de rankings), `.github/workflows/market-tick.yml` (solo el comentario «movers recalcula sus 20 enteros»).

- [ ] **Step 1:** actualizar esos textos a lo que hace ahora el tick (2 peticiones a `/items`, sin CSFloat).
- [ ] **Step 2:** `venv/Scripts/python.exe tools/dod.py` → `DOD: ALL 3 GATES GREEN`; ruff y mypy sin empeorar el ratchet.
- [ ] **Step 3:** prueba real (1-2 de cuota): `POST /internal/movers-tick` contra el back local con `X-Cap-Token` y `GET /market/movers` con dev-token. Expected: `cases[0]` es una caja con miles de ventas; `down` lo encabeza una skin cara con `priceChange30d` de decenas o cientos de $; ninguna lista con cambio 0.
- [ ] **Step 4:** commit `docs: rankings de Home en CLAUDE.md y steam.md (home-rankings)` y push.

---

### Task 6: Tipos, formato y tarjeta (front)

**Files:**
- Modify: `src/app/models/interfaces.ts` (junto a `ISkinCard`)
- Modify: `src/app/shared/utils/format-delta.ts` + su spec
- Modify: `src/app/shared/components/hot-cold/hot-cold.{ts,html,scss,spec.ts}`

**Interfaces:**
- Produces:
  - `IMoverCard extends ISkinCard { priceChange30d: number | null }`
  - `IHomeRankings { cases: ISkinCard[]; up: IMoverCard[]; down: IMoverCard[] }`
  - `formatUsdChange(usd: number | null | undefined, fmt: IPriceFormat): string` — signo `+`/`-` delante del importe convertido con `PricePipe` (valor absoluto); `null` → `'N/A'`.
  - `HotCold`: `trend` acepta `'up' | 'down' | 'flat'` (`flat` sin color); nuevo `detail = input<string | null>(null)` que se pinta pequeño junto a `percentage`.

- [ ] **Step 1: Tests:**
  - `formatUsdChange(34.16, {currency:'USD', rate:null})` → `'+$34.16'`; `(-208.33, USD)` → `'-$208.33'`; `(-100, {currency:'EUR', rate:0.9})` → `'-€90.00'`; `(null, USD)` → `'N/A'`.
  - `HotCold`: con `detail="+54.5%"` el texto aparece; con `trend="flat"` no tiene clase `up` ni `down`.
- [ ] **Step 2:** `npm run test:ci` → FAIL. **Step 3:** implementar. **Step 4:** PASS. Commit `feat(home): tipos de rankings, formato de cambio en $ y tarjeta con detalle (home-rankings)`.

---

### Task 7: Home con tres bloques (front)

**Files:**
- Modify: `src/app/features/news/services/home.service.ts` (`injectMoversQuery` → `injectHomeRankingsQuery`, tipado `IHomeRankings`, misma `queryKey: ['market', 'movers']`)
- Modify: `src/app/features/news/pages/home.{ts,html,scss,spec.ts}`
- Modify: `src/assets/i18n/es.json`, `en.json`

**Interfaces:**
- Consumes: Task 6.
- Produces (i18n, bloque `HOME`): `TOP_CASES` «Cajas más vendidas» / «Top-selling cases»; `RISING` «Skins que más suben» / «Biggest risers»; `FALLING` «Skins que más bajan» / «Biggest fallers»; `SOLD_24H` «{{n}} vendidas/24h» / «{{n}} sold/24h»; `PERIOD_30D` «30 días» / «30 days»; `SHOW_MORE` «Ver más» / «Show more»; `SHOW_LESS` «Ver menos» / «Show less». Se borran `HOT`, `COLD`, `PRICE_HIGH`, `PRICE_LOW` si nada más los usa (grep).

- [ ] **Step 1: Tests** en `home.spec.ts` (query mockeada con `{cases, up, down}`):
  - pinta tres bloques en orden: cajas, suben, bajan, con los títulos traducidos;
  - cajas: `percentage` = `SOLD_24H` con `n` = `sold24h`, `trend="flat"`;
  - suben: `percentage` = `formatUsdChange(priceChange30d)`, `detail` = `formatDelta(priceDelta30d)`, `trend="up"`; bajan igual con `trend="down"`;
  - con 10 items se ven 5; «Ver más» enseña 10; «Ver menos» vuelve a 5; con 4 no hay botón;
  - lista vacía → su bloque no se pinta (sin relleno, sin cabecera huérfana);
  - `<app-data-status endpoint="market/movers">` sigue debajo del primer bloque.
- [ ] **Step 2:** FAIL. **Step 3:** implementar; se quitan `PriceSort` y las píldoras (el orden lo da el ranking). `MoverBlock` pasa a `key: 'cases' | 'up' | 'down'` con un `expanded` signal por bloque; `INITIAL_COUNT = 5`. Tonos: cajas `accent`, suben `money`, bajan `down`. Las tarjetas siguen abriendo Market (`goToMarket(skin)`).
- [ ] **Step 4:** PASS + `npm run lint`. Commit `feat(home): cajas más vendidas y skins que suben/bajan en lugar de HOT/COLD (home-rankings)`.

---

### Task 8: Ticker (front)

**Files:** `src/app/layout/tabs/tabs.page.ts:69-77` (+ su spec si existe; si no, crear `tabs.page.spec.ts` solo para `tickerItems`).

- [ ] **Step 1: Test:** con `up` de 4 y `down` de 4, `tickerItems()` son los 3 primeros de `up` seguidos de los 3 primeros de `down`; `delta` = `formatDelta`-sin-signo de `priceDelta30d` (dos decimales + `%`) o `'N/A'`; `up` = `priceDelta30d >= 0`.
- [ ] **Step 2:** FAIL. **Step 3:** usar `injectHomeRankingsQuery` y `priceDelta30d` en vez de `hot`/`priceDelta7d`. **Step 4:** PASS. Commit `feat(ticker): la barra superior usa suben/bajan (home-rankings)`.

---

### Task 9: Cierre del front

**Files:** `CLAUDE.md` (bullet de rankings de Home), `docs/features/data-status.md` (fila Home: `market/movers` bajo el primer bloque).

- [ ] **Step 1:** textos.
- [ ] **Step 2:** `npm run lint`, `npm run test:ci` (cobertura ≥ suelo del ratchet), `npx ng build --configuration production` → todo verde. Si falla `firebase/messaging`: `npm install --no-save firebase@12.15.0` (lock intacto).
- [ ] **Step 3:** prueba en navegador contra el back local (Task 5 Step 3): Home muestra los tres bloques con datos reales, ningún 0 %, ninguna Bizon/Galil de céntimos; el ticker enseña subidas y bajadas.
- [ ] **Step 4:** commit `docs: rankings de Home (home-rankings)` y push.
