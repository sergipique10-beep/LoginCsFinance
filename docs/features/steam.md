---
feature: steam
files:
  - steam/cap_history_repo.py
  - steam/adapters/__init__.py
  - steam/adapters/_common.py
  - steam/adapters/buff_adapter.py
  - steam/adapters/csfloat_adapter.py
  - steam/adapters/fx_adapter.py
  - steam/adapters/news_adapter.py
  - steam/adapters/provider_adapter.py
  - steam/adapters/static_catalog_adapter.py
  - steam/adapters/steam_adapter.py
  - steam/api/__init__.py
  - steam/cache/__init__.py
  - steam/cache/base_cache.py
  - steam/cache/history_cache.py
  - steam/cache/image_cache.py
  - steam/cache/market_cache.py
  - steam/cache/user_cache.py
  - steam/cache/policy.py
  - steam/api/buff_client.py
  - steam/api/csfloat_client.py
  - steam/api/fx_client.py
  - steam/api/http.py
  - steam/api/news_client.py
  - steam/api/static_catalog_client.py
  - steam/api/steam_client.py
  - steam/domain/__init__.py
  - steam/domain/catalog.py
  - steam/domain/enums.py
  - steam/domain/liquidity.py
  - steam/domain/models.py
  - steam/domain/normalizers.py
  - steam/domain/rules.py
  - steam/domain/validators.py
  - steam/errors/__init__.py
  - steam/errors/domain_errors.py
  - steam/errors/handling.py
  - steam/inventory_snapshot_repo.py
  - steam/mappers/__init__.py
  - steam/mappers/item_mapper.py
  - steam/mappers/market_index_mapper.py
  - steam/mappers/movers_mapper.py
  - steam/mappers/news_mapper.py
  - steam/mappers/profile_mapper.py
  - steam/mappers/provider_mapper.py
  - steam/mappers/row_mapper.py
  - steam/price_history_repo.py
  - steam/rankings_repo.py
  - steam/router.py
  - steam/routes/__init__.py
  - steam/routes/items.py
  - steam/routes/market.py
  - steam/routes/news.py
  - steam/services/__init__.py
  - steam/services/cap_history_service.py
  - steam/services/catalog_service.py
  - steam/services/fx_service.py
  - steam/services/inventory_service.py
  - steam/services/market_service.py
  - steam/services/news_service.py
  - steam/services/price_capture_service.py
  - steam/services/pricing_service.py
  - steam/services/profile_service.py
  - steam/services/providers_service.py
  - steam/services/rankings_service.py
  - steam/utils/__init__.py
  - steam/utils/dates.py
  - steam/utils/strings.py
  - steam/utils/urls.py
---

# Módulo `steam`

Integración con steamwebapi (Steam, CSFloat y Buff van todos por ahí), el catálogo
estático de ByMykel, el tipo de cambio de frankfurter y Steam News. Sirve inventario,
perfil, mercado, rankings, histórico y noticias, y alimenta las tools del chat.

La arquitectura, los endpoints y las invariantes están en `CLAUDE.md`. Este documento
recoge lo que el refactor STEAM-REFACTOR (`docs/steam-module-refactor-plan.md`, ciclo
de Plane del mismo nombre) necesita tener por escrito:

- El frontmatter `files:` es el que lee `tools/ratchet.py` (`undocumented_files`). **Cada
  `.py` nuevo de `steam/` se añade aquí en el mismo commit**, o el DoD se pone en rojo.
- El mapa de degradaciones de abajo es el inventario de fallbacks con su decisión.

## Tests de contrato

`tests/test_steam_contract_*.py` fijan el status y el **conjunto exacto de claves** que
devuelve cada endpoint: es el contrato con el front (CS-FINANCE-ionic), que no está en
este repo. Durante el refactor son el árbitro: si un issue tiene que tocar uno de esos
tests para pasar, ha cambiado el contrato y hay que parar.

- Simulan las APIs externas **por HTTP** con la fixture `steam_api` (`tests/conftest.py` +
  `tests/steam_fake.py`): responde por sufijo de `host + path` sobre
  `app.state.http_client`, vacía las cachés de `stores.py` y quita la espera del
  `_history_limiter`. Así no dependen de en qué módulo viva cada función.
- Los bugs del mapa (CAL-11 a CAL-14) llevaban un `xfail(strict=True)` que afirmaba el
  comportamiento **correcto**; la Fase 2 del refactor (CLEAN-15) los cerró y hoy no queda
  ningún `xfail` en estos ficheros. Un bug nuevo del mapa se añade igual: test con `xfail`
  estricto, fila con su issue.

## Mapa de degradaciones

Qué hace cada flujo cuando una fuente falla. **Decisión**: *conservar* (degradación
aceptada), *UX-46* (hacerla visible al usuario, necesita al front) o el issue del bug.
"Invisible" = el cliente recibe un 200 normal sin ninguna señal.

**Log** (CLEAN-12): las degradaciones que el cliente no ve dejan una línea
`[steam-degraded] flow=<flow> reason=<reason> served=<stale|empty|fallback|error> last_hour=<n>`
(`steam/errors/handling.py`), una por fila con algo en la última columna;
`tests/test_steam_degraded_logs.py` provoca cada una. Los services la dejan vía
`degraded(flow, reason, served, data)`, que construye a la vez el `Fetched` con el
`status` que corresponde a `served` (`served_to_status`), y solo capturan `DEGRADABLE`
(`UpstreamError`, `InvalidPayload`, `UnexpectedPayload`): lo demás es un bug y sube. Las que ya se ven (cabecera, `code`,
`stale` en el cuerpo, 5xx) no llevan línea. Fuera a propósito: los mappers (una línea
por campo y tick, UX-46).

| Flujo | Disparador | Qué devuelve | ¿Lo ve el cliente? | Decisión | Log (`flow` · `reason`) |
|---|---|---|---|---|---|
| Inventario | 429 / 402 con snapshot | snapshot | `X-Inventory-Stale` | conservar (PERF-14) | `inventory` · `rate_limit`/`quota`, `served=stale` (CLEAN-18; además la línea `[inventory-429]`) |
| Inventario | 402 sin snapshot | 503 | `code: upstream_quota` | conservar (SEC-16) | — |
| Inventario | 429 sin snapshot | 429, `detail` de texto | sin `code` | CAL-14 | — |
| Inventario | 410 / 411 | snapshot con `X-Inventory-Stale` si lo hay, si no `[]`; **ni la caché ni el snapshot se pisan** | cabecera (con snapshot) | resuelto (CLEAN-15) | `inventory` · `http_410`/`http_411`, `served=stale` o `empty` |
| Inventario | JSON inválido | 502 | 502 | resuelto (CLEAN-15, `http_error_for`) | — |
| Perfil `/me` | 402 / 429 | 503 | `code: upstream_quota` / `upstream_rate_limit` | resuelto (CLEAN-15) | — |
| Perfil `/me` | 200 sin perfil | perfil en blanco **sin cachear** | invisible | resuelto (CLEAN-14) | `profile` · `empty_body` |
| Histórico (enriquecimiento) | fallo de csfloat/history | `[]` 5 min; deltas de `_inline_delta` | invisible | conservar | `history` · `reason_of(exc)` |
| `/item/history` | ventana llena / 429 | stale, o 503 + `Retry-After` | `code: upstream_rate_limit` | conservar (SEC-16) | `item_history` · `rate_limit` (solo stale) |
| `/item/history` | 402 | stale, o 503 | `code: upstream_quota` | resuelto (CLEAN-15; como el 429) | `item_history` · `quota` (solo stale) |
| `/item/history` | cuerpo no lista | `200 []` **sin cachear** | invisible | conservar (CLEAN-14): un histórico ilegible no deja sin detalle a la skin | `item_history` · `unexpected_format` |
| Lookup CSFloat/Buff | fallo | stale o `{}`, backoff 5 min | precios a `null` | conservar (PERF-17) | `market_lookup` · motivo o `backoff` |
| Proveedores | fallo | stale o `_FALLBACK_PROVIDERS` | invisible | conservar (PERF-17) | `providers` · motivo o `backoff` |
| FX | fallo / tasa fuera de 0,5–2,0 | última tasa o ninguna | `stale` en el cuerpo | conservar (UX-08) | — |
| Movers / trending | `/items` caído o ilegible | fallback a topmovers, deltas `0.0` | invisible | UX-46 (el JSON ilegible daba 500: resuelto, CLEAN-15) | `movers`/`trending` · motivo de `/items` (`invalid_json` si ilegible); `served=fallback`, o `error` sin fuentes |
| Movers / trending | topmovers cacheado caducado (`TOPMOVERS_RAW_TTL`) | no se usa: se pide market-index; si también falla, `error` | `kept_previous` en el tick | resuelto (CLEAN-15) | `topmovers` · motivo del respaldo (`served=empty`); `movers`/`trending` · `topmovers_stale` |
| `movers-tick` | ninguna fuente | conserva el snapshot anterior | `kept_previous` | conservar (CAL-10) | — |
| `trending-tick` | ninguna fuente | no inserta pero **sí purga** (`purged`) | `count: 0` | lo fija el contrato (`test_trending_tick_sin_fuentes_no_inserta_pero_purga`); el plan de CLEAN-15 proponía no purgar con `status="error"`: decisión pendiente del dueño del contrato | — |
| Trending | sticker slabs | se filtran, como en movers y búsqueda (`rules.is_sticker_slab(item)`: por `item_type` y, si no lo dice, por el nombre) | — | resuelto (CLEAN-17; CAL-14) | — |
| Búsqueda / precio | 402 | stale, o 503 | `code: upstream_quota` | conservar (SEC-16) | `search`/`item_price` · `quota` (solo stale) |
| Búsqueda / precio | 429 | 503 + `Retry-After` | `code: upstream_rate_limit` | resuelto (CLEAN-15) | — |
| Búsqueda | caché compartida con el chat | clave con namespace (`market:`/`chat:`, `search_cache_key`): cada uno la suya | — | resuelto (CLEAN-15) | — |
| `/market/index` | 402 | stale, o 503 | `code: upstream_quota` | conservar (SEC-16) | `market_index` · `quota` (solo stale) |
| `/market/prices` | 402 | stale, o 503 | `code: upstream_quota` | conservar (SEC-16) | `market_prices` · `quota` (solo stale) |
| `/market/index` | gainer sin `markethashname` / sin `change24h` | se descarta / `0.0` | invisible | resuelto (CLEAN-14) | `market_index` · `invalid_field` (solo al descartar) |
| Catálogo de imágenes | todas las fuentes caídas | backoff 5 min, `image: ""` | invisible | conservar (CAL-08) | `catalog` · `all_sources_failed` |
| Catálogo de imágenes | una fuente caída o ilegible | se cargan las demás | invisible | conservar | `catalog` · `reason_of(exc)` (`served=empty`, una por fuente) |
| Inventario | snapshot de Supabase no se puede leer/guardar | la lectura sigue; sin snapshot que servir | invisible | conservar (PERF-14, best-effort) | `inventory_snapshot` · `storage` |
| `/inventory`, `trending-tick` | `register_tracked` falla (Supabase) | la respuesta sigue; la skin no entra en la captura diaria | `tracked: 0` en el tick | conservar (best-effort) | `tracked_register` · `storage` |
| `price-tick` | el lookup de una skin falla | se marca intentada, sin punto ese día | `errors` en la respuesta | conservar (PERF-11) | `price_capture` · `reason_of(exc)` |
| Noticias | JSON que no es dict / ilegible | 502 | 502 | resuelto (CLEAN-15) | — |
| Noticias | og:image falla o la página no lo trae | `imageUrl: ""` | invisible | conservar | `news_image` · `reason_of(exc)` o `no_og_tag` (solo con URL) |
| Chat: precio / búsqueda | 402 / 429 / red / ilegible | `{"error": motivo}` (`user_message`) | el modelo lo explica | resuelto (CLEAN-15) | — |
| Chat: inventario | cualquier error | `{"error": motivo}` (`user_message`) | el modelo lo explica | resuelto (CLEAN-15); ya no hay línea: el fallo se ve | — |
| Mappers | campos ausentes | `0`, `"Base Grade"`, `True`… | invisible | UX-46 | — |

El detalle por función (con número de línea aproximado) está en el primer comentario de
CAL-09 en Plane.
