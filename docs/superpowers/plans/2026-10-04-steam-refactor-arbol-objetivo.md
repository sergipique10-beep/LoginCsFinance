# Steam refactor árbol objetivo — Implementation Plan

> **For agentic workers:** una tarea = un commit con `venv/bin/python tools/dod.py` en verde. Marca cada tarea con `- [x]` al cerrarla y escribe la «Salida de fase» (movido / igual / riesgo reducido / prueba / pendiente) al final de este fichero.

**Goal:** completar la migración de `steam/` al árbol objetivo de `docs/prompt-steam-refactor-agent.md` sin cambiar el contrato con el front, cerrando los bugs del mapa de degradaciones (CAL-11..14) en una fase propia.

**Architecture:** `api/` (transporte) → `adapters/` (payload → modelo interno validado) → `mappers/` (modelo → TypedDict de salida) → `services/` (orquestación, `Fetched[T]`) → `routes/`; `cache/` con política explícita; `errors/` con traducción única a HTTP.

**Tech Stack:** FastAPI, httpx, pytest (+ `tests/steam_fake.py`), `tools/dod.py` y `tools/ratchet.py`.

**Spec:** `docs/superpowers/specs/2026-10-04-steam-refactor-arbol-objetivo-design.md`

## Árbol final

```
steam/
├── api/                         # ex clients/ — transporte, un módulo por fuente, devuelven JSON crudo
│   ├── http.py                  #   get_json + parse_retry_after (se mueve tal cual)
│   ├── steam_client.py          #   ex steamwebapi.py (+ _SlidingWindowLimiter, _history_limiter)
│   ├── csfloat_client.py        #   history(client, name, start, end) / prices(client, params) sobre /market/csfloat
│   ├── buff_client.py           #   idem sobre /market/buff; MARKET_CLIENTS = {"csfloat": …, "buff": …}
│   ├── news_client.py           #   ex steam_news.py; fetch_og_image pasa por get_json y lanza
│   ├── static_catalog_client.py #   ex static_catalog.py
│   └── fx_client.py             #   ex fx.py (no está en el árbol del prompt, pero la fuente existe)
├── adapters/                    # NUEVO — payload externo → modelo interno validado, o UnexpectedPayload
│   ├── steam_adapter.py         #   adapt_item/adapt_items/adapt_inventory/adapt_profile/adapt_market_index/adapt_price_rows
│   ├── csfloat_adapter.py       #   adapt_history(raw, volume_key="quantity") → list[HistoryPoint]
│   ├── buff_adapter.py          #   idem (comparte _history_points con csfloat vía adapters/_common.py)
│   ├── news_adapter.py          #   adapt_news(raw) → list[NewsEntry]
│   ├── static_catalog_adapter.py#   adapt_catalog_source(raw, with_wears) → list[CatalogEntry]
│   └── provider_adapter.py      #   adapt_markets(raw) → list[ProviderInfo]  (+ fx_adapter.py: adapt_rates)
├── domain/
│   ├── models.py                #   TypedDicts de salida (ya) + modelos INTERNOS: SteamItem, ProfileData, MarketIndexData,
│   │                            #   PriceRow, ProviderInfo, NewsEntry, CatalogEntry, FxRates (dataclasses frozen)
│   ├── enums.py                 #   NUEVO: FetchStatus, Served, Market, WeaponCategory, Wear, NewsCategory
│   ├── validators.py            #   + as_float/as_int/as_bool/as_str (None si falta o no parsea; nunca 0 por defecto)
│   ├── rules.py                 #   NUEVO: ranking_eligible, diversificar, turnover, is_sticker_slab, is_readable_news
│   ├── normalizers.py           #   ex names.py (+ normalize_image_url desde mappers/items)
│   ├── liquidity.py             #   ex steam/liquidity.py (regla de negocio pura)
│   └── catalog.py               #   constantes (ya)
├── mappers/                     # puros: modelo interno → TypedDict de salida
│   ├── item_mapper.py           #   ex items.py (_map_item recibe SteamItem)
│   ├── news_mapper.py           #   ex news.py (sin is_readable_news ni _clean_news_content: van a rules/utils)
│   ├── movers_mapper.py         #   ex movers.py (sin _build_movers_from_topmovers: va a rankings_service)
│   ├── provider_mapper.py       #   NUEVO: ProviderInfo → MarketProvider (hoy inline en services/providers.py:51-69)
│   ├── market_index_mapper.py   #   ex market_index.py
│   └── row_mapper.py            #   ex rows.py
├── services/
│   ├── market_service.py        #   búsqueda, item completo, /market/index, /market/prices
│   ├── rankings_service.py      #   compute_movers/compute_trending, capture_*, enrich_trending, get_movers/get_trending
│   ├── cap_history_service.py   #   capture_cap_snapshot, get_cap_history, _downsample
│   ├── inventory_service.py     #   fetch_fresh_inventory + reintento 429 (hoy en routes/items.py)
│   ├── pricing_service.py       #   ex pricing.py
│   ├── price_capture_service.py #   ex steam/price_capture.py; expone lookup_item (público) para alerts/
│   ├── catalog_service.py       #   ex catalog.py
│   ├── news_service.py          #   ex news.py
│   ├── fx_service.py            #   ex fx.py
│   ├── profile_service.py       #   ex profile.py
│   └── providers_service.py     #   ex providers.py
├── cache/                       # NUEVO
│   ├── base_cache.py            #   TtlCache (desde stores.py) + CacheState + lookup() → (state, value)
│   ├── policy.py                #   CachePolicy(ttl, empty_ttl, fail_ttl, max_entries) y las TTL por tipo (desde stores.py)
│   ├── history_cache.py         #   item_history, topmovers_raw
│   ├── image_cache.py           #   CatalogCache: imágenes + rareza + meta/backoff (sustituye los 3 dicts planos)
│   ├── market_cache.py          #   search, item_price, market_prices, market_lookup, providers, market_index, fx
│   ├── user_cache.py            #   profile, inventory, inventory_refresh_cooldown, news
│   └── __init__.py              #   ALL_CACHES, clear_all(), stats_all()
├── errors/
│   ├── domain_errors.py         #   ex steam/errors.py (+ InvalidField(source, op, field, value))
│   ├── handling.py              #   ex degraded.py (log_degraded, reason_of) + http_error_for(exc) (traducción única a HTTP)
│   └── __init__.py              #   re-exporta todo: `from steam.errors import X` sigue funcionando (alerts/, tools/)
├── utils/
│   ├── strings.py               #   _clean_news_content (re-exportado desde news_mapper para rag/ y notifications/), lower_key
│   ├── dates.py                 #   iso_day(createdat), hour_floor, today
│   └── urls.py                  #   steam_cdn_url, is_http_url
├── routes/                      # sin cambios de forma; solo imports y http_error_for
├── cap_history_repo.py, price_history_repo.py, rankings_repo.py, inventory_snapshot_repo.py, router.py  # se quedan
```

Fuera de alcance (issues aparte, no refactor): añadir `weapontype`/`exterior`/`variants` a `_MOVERS_SELECT` (cambia el payload pedido a steamwebapi), exponer `Fetched.status` al front (UX-46), mover los repos de Supabase a un paquete, migrar a Redis (CAL-04).

---

## Reglas comunes a todas las tareas

- **Una tarea = un commit** con DoD en verde (`venv/bin/python tools/dod.py` → `DOD: ALL 3 GATES GREEN`). Push al cerrar cada fase.
- **Mover = `git mv`** (conserva historial) + actualizar imports en `steam/`, `tests/`, y las **líneas de import** de los consumidores externos (`main.py`, `tools/`, `alerts/`, `predict/`, `rag/`, `notifications/`, `auth/`). Nada más de esos paquetes se toca.
- Cada fichero nuevo/movido: alta en `docs/features/steam.md` (`files:`), test que lo importe, y `venv/bin/python tools/ratchet.py --bless` en el mismo commit.
- Los tests de contrato `tests/test_steam_contract_*.py` **no se editan**, salvo quitar `xfail` en la Fase 2.
- Supresiones puntuales: `# noqa: XXX — motivo`. Código nuevo entra con 0 ruff / 0 mypy.
- Comentarios: se conservan los que explican una invariante o una medición (p. ej. `pricereal`, `_MOVERS_SELECT` con `prices`, PERF-14); se quitan los de diario («antes vivía en…», «como antes») en la Fase 6, no antes.
- **Salida tras cada fase** (lo exige el prompt): sección en `docs/superpowers/plans/2026-10-04-steam-refactor-arbol-objetivo.md` con *movido / igual / riesgo reducido / prueba / pendiente*.

---

## Fase 0 — Línea base (CLEAN-13)

### Tarea 0.1 — Entorno y medición
- `python3 -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-dev.txt`.
- `venv/bin/python tools/dod.py` → debe dar 3 gates en verde con la baseline actual (ruff 73, mypy 61, coverage_floor 86). Guardar la salida en `logs/dod/` (lo hace solo). Si **no** está en verde, parar y reportar: el refactor no arranca sobre rojo.
- Anotar nº de tests (630) y de `xfail` (14).

### Tarea 0.2 — Documento de diagnóstico
- Crear `docs/superpowers/specs/2026-10-04-steam-refactor-arbol-objetivo-design.md` con la tabla de huecos de arriba, el mapa de riesgos, la lista de consumidores externos y de monkeypatch por string, y el árbol final. Copiar este plan a `docs/superpowers/plans/2026-10-04-steam-refactor-arbol-objetivo.md` en el formato del repo (`# … Implementation Plan`, `**Goal/Architecture/Spec**`, `## File Structure`, `### Task N` con `- [ ]`).

### Tarea 0.3 — Guardias arquitectónicas que hoy faltan
- `tests/test_steam_layers.py`: añadir, con el mismo `_imports()` por AST, las reglas del orden de dependencias de `CLAUDE.md` que no están comprobadas: `domain/*` no importa `steam.mappers|services|clients|api|cache|stores`; `mappers/*` no importa `services|clients|api|stores|cache`; `adapters/*` solo importa `domain|errors|utils`; `cache/*` no importa nada de `steam` salvo `errors`. Al principio pasan (los directorios nuevos no existen); son la red para las fases siguientes.
- `tests/test_stores_ttl_cache.py:84-92`: ampliar el guardia `cached\[1\]` a `\w+\[1\]\s*[<>]` sobre `steam/` y `tools/` (hoy un `entry[1]` lo esquiva).
- **Fixtures de payload**: no existe `tests/fixtures/`. Crear `tests/fixtures/steamwebapi/{items,inventory,profile,market_index,market_prices,history_csfloat,history_legacy,info_markets}.json`, `tests/fixtures/{bymykel_skins,bymykel_stickers,steam_news,frankfurter}.json`, construidos a partir de los dicts que ya usan `tests/test_steam_contract_market.py:25-28`, `tests/test_steam_flows_mappers.py`, `tests/test_steam_contract_ticks.py` (`TOPMOVERS`) y los campos que leen los mappers (lista en la auditoría). Fixture `payload(name)` en `conftest.py`. Son la entrada de los tests de adapters de la Fase 1.
- Commit: `test(steam): guardias de capas, fixtures de payload y diagnóstico (CLEAN-13)`.

---

## Fase 1 — Contratos de integración: `api/`, `adapters/`, `errors/` (CLEAN-14)

### Tarea 1.1 — `steam/errors/` paquete
- `git mv steam/errors.py steam/errors/domain_errors.py`; `git mv steam/degraded.py steam/errors/handling.py`.
- `steam/errors/__init__.py` re-exporta **todo** lo público de ambos: ningún `from steam.errors import …` cambia (hay 9 fuera de `steam/`). Actualizar `from steam.degraded import` en `tools/inventory_tools.py:74` y en `steam/`.
- Añadir `InvalidField(UnexpectedPayload)` con `source`, `operation`, `field`, `value` en el mensaje (lo usan los adapters). `reason_of` devuelve `"invalid_field"` para ella.
- Tests: `tests/test_steam_errors.py` (jerarquía, `reason_of`, mensaje de `InvalidField`). Mover `tests/test_steam_degraded_logs.py` imports.

### Tarea 1.2 — `steam/api/` (ex `clients/`)
- `git mv steam/clients steam/api`; renombrar `steamwebapi.py→steam_client.py`, `steam_news.py→news_client.py`, `static_catalog.py→static_catalog_client.py`, `fx.py→fx_client.py`. `http.py` se queda.
- Crear `csfloat_client.py` y `buff_client.py`: `history(client, name, start, end, *, timeout)` y `prices(client, params, *, timeout)` que delegan en `steam_client.market_history/market_prices` con el market fijo, y `MARKET_CLIENTS: dict[str, ModuleType]` en `api/__init__.py` para que `get_item_history` elija por market sin pasar strings al cliente.
- `news_client.fetch_og_image`: deja de tragarse errores. Pasa por `get_json`‑like (`get_text`) y **lanza** los errores tipados; el `""` lo decide `news_service` con `log_degraded("news_image", reason_of(exc), "empty")` (hoy la línea no lleva motivo).
- Actualizar imports: `steam/`, `tests/test_steam_client.py`, `tests/test_steam_external_clients.py`, `tests/test_steam_key_header.py`, `tests/test_perf03_limiter_timeout.py`, `tests/test_price_capture.py`, `tests/test_news_cache.py`, `tests/test_steam_flows_mappers.py`, `tests/conftest.py:118`. `tests/test_steam_layers.py`: la regla de rutas pasa a `steam.api`.

### Tarea 1.3 — Modelos internos y validadores de valor
- `domain/models.py`: dataclasses `frozen=True` **internas** (no son contrato con el front): `SteamItem` (todos los campos que hoy lee `_map_item`, tipados `float | None` / `int | None` / `bool | None`; conserva `raw_prices: tuple[PriceQuote, …]`), `ProfileData`, `MarketIndexData(history, gainers, losers, turnover24h, sold24h)`, `PriceRow(name, price)`, `ProviderInfo(id, name, logo)`, `NewsEntry`, `CatalogEntry(name, image, rarity, color, wears, stattrak, market_hash_name)`, `FxRates`.
- `domain/validators.py`: `as_float(value, *, field, source) -> float | None`, `as_int`, `as_bool`, `as_str`. Devuelven `None` si falta o no es convertible; **nunca** `0`/`""`. Un tipo imposible (p. ej. un dict donde va un número) lanza `InvalidField`. `canonical_price` pasa a usarlos (hoy `float(item.get(key) or 0)`).
- Tests: `tests/test_domain_validators.py` (ampliar), `tests/test_steam_models.py` (los TypedDict de salida no cambian de claves).

### Tarea 1.4 — Adapters
- `steam/adapters/_common.py`: `require_list(raw, *, source, op)`, `require_dict`, `history_points(raw, volume_key)` (una sola implementación del mapping de `HistoryPoint`, hoy duplicado en `pricing.py`).
- `steam_adapter.py`: `adapt_items(raw) -> list[SteamItem]` (acepta el formato plano y el anidado `{"item": …}`), `adapt_inventory`, `adapt_item` (unwrap de `/item`, hoy `price_capture._lookup_item:47`), `adapt_profile` (lista vacía → `None`, no `{}`), `adapt_market_index` (sin `.get("topmovers", {})` silencioso: lista ausente → `[]` **con** `reason`; gainer sin `markethashname` se descarta con `log_degraded("market_index","invalid_field","partial")`, no `KeyError`), `adapt_price_rows`.
- `csfloat_adapter.py` / `buff_adapter.py`: `adapt_history`. `news_adapter.py`: `adapt_news` (`appnews.newsitems` obligatorio; dict no → `UnexpectedPayload`). `static_catalog_adapter.py`: `adapt_catalog_source(raw, with_wears)`. `provider_adapter.py`: `adapt_markets` (la cadena de 10 alias de logo pasa aquí y se documenta). `fx_adapter.py`: `adapt_rates`.
- **Contrato de cada adapter**: entrada `Any` (lo que devolvió `get_json`), salida modelo interno, errores `UnexpectedPayload`/`InvalidField`. Sin caché, sin HTTP, sin `log`, sin fallback.
- Tests `tests/test_invalid_payloads.py`: por adapter, payload normal (fixture), incompleto (campo ausente → `None` en el modelo), implausible (string donde va número → `InvalidField`), forma incorrecta (dict/lista cruzados → `UnexpectedPayload`), item variante con nombre inusual (Souvenir, ★ StatTrak™, Sticker Slab, nombre en portugués de `marketname`), provider sin metadata, noticia con `contents` vacío.

### Tarea 1.5 — Mappers sobre el modelo interno
- `git mv` a `item_mapper.py`, `news_mapper.py`, `movers_mapper.py`, `market_index_mapper.py`, `row_mapper.py`; crear `provider_mapper.py`.
- `_map_item(item: SteamItem) -> SkinCard`: mismo JSON de salida (contrato), pero la entrada es el modelo. El colapso a `0` de `sold24h`, `offerVolume`… **se queda en la salida** porque es contrato (`test_steam_contract_rows.py:76-78`), y `compute_liquidity` recibe el `SteamItem` con sus `None` intactos (misma semántica que hoy con el dict crudo). Documentar en el docstring: «la distinción None/0 vive en `SteamItem`; la tarjeta es presentación».
- `_build_movers_from_topmovers` sale del mapper (es regla + log) → `rankings_service` en Fase 5; hasta entonces se queda, señalado.
- `services/*` pasan a llamar `adapter → mapper`; desaparecen los `isinstance(data, list)` sueltos (6) y los dos bloques duplicados de `HistoryPoint`.
- Actualizar `tools/market_tools.py:104,161,208,219` (imports de `_map_item`, `_row_to_item`) y los tests que importan mappers.
- Tests: `tests/test_item_mapping.py` (renombrar/ampliar `test_steam_flows_mappers.py`): `_map_item` sobre `SteamItem` con campos a `None` da `0` en la tarjeta **y** `compute_liquidity` recibe `None`.
- Commit por tarea: `refactor(steam): … (CLEAN-14)`.

**Salida de fase**: ningún service lee claves del JSON crudo; `grep -rn "isinstance(data, list)" steam/services` → 0; `grep -rn "createdat" steam/services` → 0.

---

## Fase 2 — Validación, errores explícitos y los bugs del mapa (CLEAN-15)

### Tarea 2.1 — Traducción única a HTTP — [x]
- `errors/handling.py:http_error_for(exc, *, timeout_status=502) -> HTTPException`: `QuotaExhausted`→503 `UPSTREAM_QUOTA_DETAIL`; `RateLimited`/`HistoryBusy`→503 `UPSTREAM_RATE_LIMIT_DETAIL` + `Retry-After`; `SourceTimeout`→`timeout_status` (504 en index/prices, 502 en búsqueda: hoy difieren a propósito); `SourceUnavailable`/`UpstreamError`→502; `InvalidPayload`/`UnexpectedPayload`/`InvalidField`→502 con `detail` (hoy `InvalidPayload` da 500: CAL-14). `handling.py` **sí** puede importar `fastapi` (no es un service; la guardia de capas excluye `errors/`).
- Rutas (`routes/items.py`, `routes/market.py`, `routes/news.py`) sustituyen sus 5 bloques `except` por `http_error_for`. Los status que hoy están bien no cambian; los que cambian son exactamente los `xfail` CAL-14.

### Tarea 2.2 — Cerrar CAL-14 (9 xfail) — [x]
Cada punto quita **un** `xfail(strict=True)`; el test afirma el comportamiento correcto y pasa a ser la regresión:
- `/me` 402 → 503 `upstream_quota` (`test_me_402_es_503_upstream_quota`); perfil vacío **no se cachea** (`profile_service` devuelve `Fetched(blank,"error","empty_body")` sin `put`).
- `/inventory` JSON inválido → 502 (`http_error_for`).
- `/item/history` 402 sin caché → 503 `upstream_quota`; cuerpo no‑lista → `UnexpectedPayload` desde el adapter, **sin** `put` de 23 h.
- `/news/cs2` JSON no‑objeto → 502 (adapter).
- `/market/index` gainer sin nombre → se descarta, no 500 (adapter).
- Ticks movers/trending con `/items` JSON inválido → no 500: `_ranking_items` captura `InvalidPayload` como ya captura red, y sigue al fallback con `reason="invalid_json"`.
- Tools del chat: `consultar_precio_skin` 402 y `ver_inventario` 403/429/red devuelven `{"error": <motivo legible>}` — el cambio es en `steam/services` (lanzar tipado) y en **la línea de `except`** de `tools/*_tools.py`, nada más.

### Tarea 2.3 — Cerrar CAL-11, CAL-12, CAL-13 (3 xfail) — [x]
- CAL-11: `tools/market_tools` escribe en `_search_cache` con clave del usuario sin liquidez. Fix en `steam`: `market_service.search_items(..., cache_namespace="chat")` → clave `f"{ns}:{q}"`; la búsqueda web usa `"market"`. Línea de import/llamada en tools.
- CAL-12: `_topmovers_raw_cache` se lee `stale` sin mirar edad → `fresh()` con `TOPMOVERS_RAW_TTL = MARKET_INDEX_CACHE_TTL`; si caducó, `Fetched([], "error", "topmovers_stale")` + `log_degraded`.
- CAL-13: 410/411 del inventario no pisa el snapshot ni la caché: `inventory_service` devuelve `Fetched([], "error", "http_410")` y la ruta sirve el snapshot con `X-Inventory-Stale` si existe; sin snapshot, `[]` como hoy.

### Tarea 2.4 — Fallbacks trazables — [x]
- Cada `except Exception` de `steam/` (13) pasa a capturar la tupla tipada `(UpstreamError, InvalidPayload, UnexpectedPayload)`; lo que no sea eso **se propaga** (un `KeyError` es un bug, no una degradación). Donde el bucle debe seguir (catálogo por fuente, price-tick por skin, `register_tracked` best-effort), se captura tipado y se llama a `log_degraded` con `reason_of` — hoy el fallo parcial del catálogo y el `_topmovers` con status no dejan línea.
- `rankings`: `capture_trending` **no purga** si `compute_trending` devolvió `status="error"`.
- `Fetched` en los services que aún no lo devuelven: `news_service`, `profile_service`, `inventory_service`, `fetch_history_for_item`, `_fetch_market_price_lookup`. Las rutas siguen devolviendo `.data` (UX-46 queda fuera).
- `domain/enums.py:FetchStatus`/`Served`: `log_degraded` acepta `Served` y `Fetched.status` se deriva de él en un solo sitio (`handling.served_to_status`).
- Tests: ampliar `tests/test_steam_degraded_logs.py` con las líneas nuevas (catálogo parcial, topmovers con status, og:image con motivo). `tests/test_market_service.py`, `tests/test_news_service.py` (fuente caída, 402, 429, payload corrupto → status/reason correctos).
- Actualizar el **mapa de degradaciones** de `docs/features/steam.md`: las filas CAL-11..14 pasan a «resuelto (CLEAN-15)» con su nuevo comportamiento.

**Salida de fase**: `grep -rn "except Exception" steam/` → 0; `grep -rn "xfail" tests/test_steam_contract_*` → 0.

---

## Fase 3 — Caché encapsulada (CLEAN-16)

### Tarea 3.1 — `steam/cache/base_cache.py` y `policy.py`
- Mover `TtlCache` desde `stores.py` (`git mv` no aplica: es parte de un fichero; se corta y `stores.py` queda con auth + `_leetify_cache` + un `from steam.cache.base_cache import TtlCache  # compat` que se retira en Fase 6).
- `CacheState = Enum("fresh","stale","empty","error")`; `TtlCache.lookup(key, now) -> tuple[CacheState, Any]`: `fresh` si en TTL, `empty` si no hay entrada, `stale` si hay entrada caducada, `error` si `in_backoff`. Los `fresh/stale/put/mark_failed/in_backoff` actuales se conservan (son la API que CLEAN-09 fijó).
- `policy.py`: `@dataclass(frozen=True) CachePolicy(ttl, empty_ttl=None, fail_ttl=0, max_entries=None)` y las constantes (`PROFILE`, `INVENTORY`, `MARKET_INDEX`, `ITEM_HISTORY`, `SEARCH`, …) **con los valores actuales de `stores.py`**, una por tipo de dato, cada una con su motivo (el comentario que ya tienen). `TtlCache.from_policy(policy)`. `invalidate(key)` y `invalidate_prefix(prefix)` (política por clave / por fuente).
- Tests `tests/test_cache_policy.py`: expiración, `empty_ttl`, stale, backoff, `max_entries`, `lookup()` devuelve el estado correcto, `invalidate_prefix`, `stats()`.

### Tarea 3.2 — Las cachés por dominio
- `history_cache.py`, `market_cache.py`, `user_cache.py`: instancias `TtlCache.from_policy(...)` con los **mismos nombres** (`_item_history_cache`, `_search_cache`…) para que el cambio en services/tests sea solo el import.
- `image_cache.py`: `class CatalogCache` que sustituye `_item_image_cache`, `_item_rarity_cache` y `_image_cache_meta`: `image_for(candidates)`, `rarity_for(name)`, `register(keys, image, rarity)`, `mark_loaded(n)`, `mark_failed()`, `is_fresh_or_backoff()`, `__len__`, `clear()`. `catalog_service` deja de indexar dicts.
- `_inventory_refresh_cooldown` → `TtlCache(INVENTORY_REFRESH_COOLDOWN)` en `user_cache.py` (hoy dict con `monotonic` a mano en `routes/items.py:206,219`).
- `steam/cache/__init__.py`: `ALL_CACHES: dict[str, TtlCache | CatalogCache]`, `clear_all()`, `stats_all() -> dict[str, dict]`.
- `tests/conftest.py:121-124` (`_clear_caches` recorre `vars(stores)`) → `steam.cache.clear_all()`; `client` fixture idem. Los ~10 tests que envejecen entradas escribiendo `(valor, ts)` a mano siguen funcionando (`TtlCache` sigue siendo `dict`).
- Observabilidad: `cap_history_service.capture_cap_snapshot` (horario) escribe una línea `[steam-cache] name=<n> entries= hits= misses= stale_served=` por caché vía `stats_all()`. Sin endpoint nuevo.
- Tests: `tests/test_image_cache.py` y `tests/test_lookup_negative_cache.py` se adaptan a `CatalogCache`/imports; `tests/test_stores_ttl_cache.py` → `tests/test_cache_policy.py`.

**Salida de fase**: `grep -rn "_cache" stores.py` → solo `_leetify_cache`; `grep -rn "dict\[str, str\] = {}" steam/` → 0.

---

## Fase 4 — Reglas de negocio sin strings sueltos (CLEAN-17)

### Tarea 4.1 — `domain/enums.py`
- `Market(str, Enum)`: `STEAM, CSFLOAT, BUFF`; `TRACKED_MARKETS`, `VALID_MARKETS`, `HISTORY_MARKETS` de `domain/catalog.py` pasan a derivarse del enum (mismos valores). `WeaponCategory`, `Wear` (5 valores de `WEAR_NAMES`), `NewsCategory` (`BLOG, VALVE, HLTV, LIQUIPEDIA, ESPORTS, OTHER` con su color), `FetchStatus`, `Served`.

### Tarea 4.2 — `domain/rules.py` y `domain/normalizers.py`
- `git mv steam/domain/names.py steam/domain/normalizers.py` (+ `normalize_image_url` desde `item_mapper`, con `utils/urls.py` debajo). `git mv steam/liquidity.py steam/domain/liquidity.py` (`compute_liquidity(item: SteamItem)`).
- `rules.py`: `ranking_eligible`, `plausible_ratio`, `plausible_fx_rate` (desde validators, que se queda con validación de **valor**), `diversificar`, `turnover` (desde `services/market.py:112-162`), `is_sticker_slab(item: SteamItem)` — **decide por `item_type` primero** (`"sticker slab"` normalizado) y solo si falta usa el nombre; se aplica también en `compute_trending` (hoy no: fila «Trending · sticker slabs» del mapa, CAL-14). `is_readable_news(entry: NewsEntry)` desde `news_mapper`. `news_category(feedname, feedlabel) -> NewsCategory` sustituye los 5 substrings de `news_mapper.py:60-68` (misma salida, tabla explícita + `OTHER`).
- `weapon_category`: la tabla `WEAPON_CATEGORY` se queda como catálogo; los 8 substrings de respaldo (`"glove"`, `"knife"`, …) pasan a una tabla `WEAPON_CATEGORY_FALLBACK` explícita en `domain/catalog.py` con test; cuando `SteamItem.weapon_type` viene, gana siempre (ya es así).
- `test_domain_names.py:85-95` (los prefijos solo en `domain/`) sigue valiendo: añadir `sticker slab` por `item_type` y `"glove"|"knife"|"bayonet"` a la regex de la guardia (solo pueden aparecer en `domain/`).
- Actualizar `tools/market_tools.py:160` (`is_sticker_slab`) y `tests/test_market_diversidad.py`, `tests/test_liquidity.py`, `tests/test_domain_names.py` → `tests/test_domain_rules.py` + `tests/test_domain_normalizers.py`.

**Salida de fase**: `grep -rnE "in name|startswith\(|\.lower\(\) ==" steam/services steam/mappers` → 0 (todo en `domain/`).

---

## Fase 5 — Services por responsabilidad (CLEAN-18)

### Tarea 5.1 — Renombrar a `*_service.py`
- `git mv` de los 8 services; actualizar `main.py:21`, `tools/`, `predict/service.py:41`, tests con monkeypatch por string (`steam.services.pricing.fetch_history_for_item` → `steam.services.pricing_service.…`, `main.fetch_static_images` se mantiene porque `main.py` lo importa por nombre).

### Tarea 5.2 — Partir `market_service.py` (753 líneas)
- `rankings_service.py`: `compute_movers`, `compute_trending`, `_ranking_items`, `_topmovers`, `get_movers`, `get_trending`, `capture_movers`, `capture_trending`, `enrich_trending`, `_build_movers_from_topmovers` (desde el mapper), constantes `_MOVERS_SELECT` (con `prices`, load-bearing), `_TRENDING_*`, `_ENRICH_BATCH`.
- `cap_history_service.py`: `capture_cap_snapshot`, `get_cap_history`, `_downsample`, `_parse_ts`, `_CAP_TF_MAP`, `_CAP_BUCKET_MAP`, `_CAP_FIELDS`.
- `market_service.py` conserva búsqueda, `get_item_full`, `get_market_index`, `get_market_prices`, `_stale_or_raise`.
- `routes/market.py` cambia solo los módulos a los que llama. Tests: `tests/test_cap_history_downsample.py`, `tests/test_movers_tick_vacio.py`, `tests/test_market_signal_filter.py`, `tests/test_market_turnover.py`, `tests/test_trending_track_top.py`.

### Tarea 5.3 — `inventory_service` absorbe el 429
- Mover de `routes/items.py:79-177` a `inventory_service.py`: `_retry_inventory`, `_retry_tasks`, `_backoff`, `_schedule_retry`, `_store` (snapshot), `_log_429` (→ usa `log_degraded("inventory","rate_limit","stale")` además de la línea `[inventory-429]`, que se conserva por los `grep` documentados). La ruta queda en: auth, rate limit, llamar `inventory_service.get_inventory(client, steam_id, force=)`, poner cabeceras `X-Inventory-Stale`/`X-Inventory-Captured-At` según `Fetched.status/reason`. Tests `tests/test_inventory_429.py` (16) y `tests/test_inventory_refresh.py` cambian solo el objetivo del monkeypatch.

### Tarea 5.4 — `price_capture_service`
- `git mv steam/price_capture.py steam/services/price_capture_service.py`; `_lookup_item` → `lookup_item` público (usa `steam_adapter.adapt_item`); `alerts/service.py:34,145,190` y `main.py:106` cambian el import. `tests/test_price_capture.py`, `tests/test_price_cap_timeout.py`, `tests/test_alerts_service.py` idem.

**Salida de fase**: ningún fichero de `steam/services/` > 400 líneas; `routes/items.py` < 100 líneas; `grep -rn "_lookup_item" alerts/` → 0.

---

## Fase 6 — Limpieza final (CLEAN-19)

### Tarea 6.1 — `steam/utils/`
- `strings.py` (`_clean_news_content` — `news_mapper` lo re-exporta para `rag/ingest.py:14` y `notifications/service.py:14`, que no se tocan —, `lower_key`), `dates.py` (`iso_day`, `hour_floor`, `today` inyectable para que `_delta_from_history` deje de depender del reloj en tests), `urls.py`.

### Tarea 6.2 — Comentarios y logs
- Quitar comentarios de diario (`«antes vivía en»`, `«como antes»`, referencias a CLEAN-xx ya cerrados en docstrings de módulo) manteniendo los de invariante/medición. Unificar prefijos de log por fuente (`[steam-client]`, `[catalog]`, `[item-history]`…) y que toda degradación tenga su `[steam-degraded]`.
- Retirar los `# compat` de `stores.py`.

### Tarea 6.3 — Documentación y cierre
- `CLAUDE.md`: sección «Module structure» y «Dependency order» con el árbol final; «In-memory stores» → apunta a `steam/cache/`; mapa de degradaciones en `docs/features/steam.md` al día; `docs/steam-module-refactor-plan.md` marcado como ejecutado con enlace al plan/spec de 2026-10-04.
- `python tools/ratchet.py --bless` final; comprobar que `coverage_floor` solo sube.
- Resumen *movido / igual / riesgo reducido / prueba / pendiente* de las 6 fases en el plan del repo.

---

## Verificación

Por tarea:
```bash
venv/bin/python tools/dod.py --fast        # compile + ratchet (segundos)
venv/bin/python -m pytest tests/ -q        # suite completa (630 + nuevos)
venv/bin/python tools/dod.py               # DOD: ALL 3 GATES GREEN antes de cada commit
```
Por fase, además:
- **F1**: `grep -rn "isinstance(data, list)\|createdat" steam/services` → 0; `tests/test_invalid_payloads.py` cubre los 9 escenarios del prompt (payload incompleto, implausible, fuente caída, cache stale, variante rara, provider sin metadata, news vacía, precio fuera de ratio, rate limit parcial).
- **F2**: 0 `xfail` en `tests/test_steam_contract_*`; `grep -rn "except Exception" steam/` → 0; cada fila del mapa de degradaciones con «Log» tiene su test en `test_steam_degraded_logs.py`.
- **F3**: `steam.cache.stats_all()` devuelve una entrada por caché; `conftest` no toca `stores` para `steam/`.
- **F4**: guardia de `test_domain_normalizers.py` ampliada en verde.
- **F5**: `tests/test_steam_layers.py` con las reglas nuevas en verde.
- **End-to-end** (una vez por fase, con el venv): `venv/bin/python -m uvicorn main:app --port 8000` y `curl localhost:8000/` → 200; `GET /news/cs2` (sin auth) → 200 con las claves de `NewsItem`; `POST /auth/dev-token` 404 salvo `DEBUG=true` (smoke de `deploy-smoke.yml`).
- CI (`.github/workflows/ci.yml`) debe estar en verde en cada push de fase; Render despliega solo tras CI.


---

## Salida de fase

### Fase 0 (CLEAN-13, commit `8826523`)
- **Movido:** nada. **Igual:** todo el comportamiento.
- **Riesgo reducido:** las capas nuevas nacen con guardia de dependencias por AST; `x[1] <` ya no esquiva la guardia de `TtlCache`; hay payloads de ejemplo versionados (`tests/fixtures/`).
- **Prueba:** `tests/test_steam_layers.py::test_orden_de_dependencias`, `tests/test_steam_flows_mappers.py::test_fixture_*`. DoD en verde (807 tests, cobertura 89 %).
- **Pendiente:** nada.

### Fase 1 (CLEAN-14, commits `c16c270`..`08df69f`)
- **Movido:** `steam/errors.py` → `errors/domain_errors.py`, `steam/degraded.py` → `errors/handling.py`; `steam/clients/` → `steam/api/` (steam_client, news_client, static_catalog_client, fx_client, http); `steam/mappers/{items,news,movers,market_index,rows}.py` → `*_mapper.py`.
- **Nuevo:** `api/csfloat_client.py`, `api/buff_client.py`, `MARKET_CLIENTS`; `http.get_text`; `steam/adapters/` (9 ficheros); modelos internos en `domain/models.py`; `as_float/as_int/as_bool/as_str` e `InvalidField`; `mappers/profile_mapper.py`, `mappers/provider_mapper.py`; `tests/test_invalid_payloads.py` (56 casos), `tests/test_steam_errors.py`.
- **Igual:** el contrato JSON de todas las rutas (los `tests/test_steam_contract_*` no se han tocado salvo quitar 4 `xfail`); la cadena de precio, las reglas de plausibilidad, las cachés y sus TTL, el limiter, los flows de `[steam-degraded]`.
- **Riesgo reducido:** ningún service lee claves del JSON crudo (`grep "isinstance(data, list)" steam/services` → 0 salvo un log); el mapping de `HistoryPoint` tiene una sola implementación; una forma inesperada nombra fuente y operación; un campo con tipo imposible es `InvalidField`, no un 500 por `ValueError`; `fetch_og_image` ya no traga errores sin motivo.
- **CAL-14 cerrados de paso (4 xfail fuera):** gainer sin `markethashname` en `/market/index` (descartado, no 500); perfil vacío no se cachea; `/item/history` con cuerpo ilegible no cachea 23 h; `change24h` ausente = 0.0. Quedan 13 `xfail` (CAL-11, CAL-12, CAL-13 y el resto de CAL-14: status codes) para la Fase 2.
- **Prueba:** DoD en verde (873 tests, cobertura ~90 %, ruff 73 → 71, mypy 61 → 56).
- **Pendiente para la Fase 2:** `http_error_for` (InvalidPayload → 502, 402/429 con `code` en /me, búsqueda e /item/history); los 13 `xfail`; `except Exception` → tuplas tipadas (13 sitios); `Fetched` en news/profile/inventory/history/lookup; `capture_trending` no purga con `status="error"`.
- **Deuda consciente dejada:** `canonical_price` y `price_capture._lookup_item` siguen sobre el dict crudo de `/item` (8 mocks en tests de alerts y price-capture; se cambia en la Fase 5.4); `tools/market_tools` llama al adapter antes de `_map_item` (3 líneas por tool); `domain/names.py` y `liquidity.py` siguen en su sitio hasta la Fase 4.

### Fase 2 (CLEAN-15, commits `4dd2540`..HEAD)
- **Movido:** nada (`git mv` no aplica). **Nuevo:** `steam/domain/enums.py` (`FetchStatus`, `Served`);
  en `errors/handling.py`: `http_error_for`, `SOURCE_ERRORS`, `DEGRADABLE`, `degraded`, `served_to_status`,
  `user_message`; `StorageError` en `domain_errors.py` y `cap_history_repo.storage_call`;
  `market.search_cache_key`; `stores.TOPMOVERS_RAW_TTL`; `tests/test_market_service.py`,
  `tests/test_news_service.py`.
- **Igual:** el contrato JSON de todas las rutas y todos los status que no eran `xfail`; las cachés y sus
  TTL (el de topmovers ya existía, ahora se respeta); el limiter; el `[]` de `/item/history` con cuerpo
  ilegible (conservado a propósito, fila del mapa); el 429 de `/inventory` sin snapshot (sigue siendo 429 con
  `detail` de texto: cambiarlo es contrato, no estaba en los `xfail`).
- **Cambiado (los 13 `xfail`):** `InvalidPayload` → 502 en /inventory y /news/cs2 (antes 500); 402 en /me e
  /item/history → 503 `upstream_quota`; 429 en /me y búsqueda → 503 `upstream_rate_limit` + `Retry-After`;
  ticks con `/items` ilegible → fallback (`reason=invalid_json`); tools del chat → `{"error": motivo}`
  (CAL-14). `_search_cache` con namespace `market:`/`chat:` (CAL-11). Topmovers caducado no es respaldo:
  `error` + `topmovers_stale` y el movers-tick conserva el snapshot (CAL-12). 410/411 del inventario →
  `Fetched([], "error", "http_410")`: snapshot con `X-Inventory-Stale` si lo hay, `[]` si no, y nunca se
  pisa caché ni snapshot (CAL-13).
- **Riesgo reducido:** una sola correspondencia error → HTTP (`http_error_for`) en vez de 8 bloques `except`
  divergentes; `grep -rn "except Exception" steam/` → **0** (13 → 0): los services capturan `DEGRADABLE`
  y los best-effort de Supabase `StorageError`, así que un `KeyError` o un `TypeError` ya sale como 500 en
  vez de disfrazarse de degradación; `Fetched` en todos los services con camino de degradación (profile,
  news, inventory, history, lookup) con el `status` derivado de `served` en un solo sitio; cuatro
  degradaciones que no dejaban línea ahora la dejan (`topmovers`, `catalog` parcial, `tracked_register`,
  `inventory_snapshot`, `price_capture`), cada una con su test.
- **Prueba:** DoD en verde (942 tests, cobertura 90 % → `coverage_floor` 86 → 90; ruff 71, mypy 56, sin
  cambios). `grep -rn "xfail" tests/test_steam_contract_*` → **0**.
- **Desvíos respecto al plan:** (1) 2.1 y 2.2 van en un commit: `http_error_for` cambia exactamente los
  status que los `xfail(strict=True)` afirmaban, así que separarlos dejaba el DoD en rojo. (2) CAL-11 se
  resolvió con `search_cache_key(namespace, query)` y no con `search_items(..., cache_namespace=)`:
  `search_items` no toca la caché (cada llamador guarda su resultado mapeado, con `select` distinto), así
  que el parámetro no tenía dónde actuar. (3) `capture_trending` **sigue purgando** con `status="error"`:
  el contrato `test_trending_tick_sin_fuentes_no_inserta_pero_purga` afirma `purged: 3` sin fuentes, y los
  tests de contrato no se editan; queda como decisión para el dueño del contrato (fila «`trending-tick` ·
  ninguna fuente» del mapa). (4) `errors/handling.py` importa `steam/domain/{enums,models}`
  (hojas sin imports internos), reflejado en el orden de dependencias de `CLAUDE.md`.
- **Pendiente para la Fase 3:** `Fetched.status` sigue sin exponerse al front (UX-46); las cachés se
  siguen tocando desde `stores` (CLEAN-16 las encapsula); `_news_cache`, `_profile_cache` y compañía siguen
  siendo `TtlCache` de módulo.
