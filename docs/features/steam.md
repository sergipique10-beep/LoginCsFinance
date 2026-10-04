---
feature: steam
files:
  - steam/cap_history_repo.py
  - steam/inventory_snapshot_repo.py
  - steam/liquidity.py
  - steam/mappers.py
  - steam/market_rows.py
  - steam/price_capture.py
  - steam/price_history_repo.py
  - steam/rankings_repo.py
  - steam/router.py
  - steam/routes/__init__.py
  - steam/routes/items.py
  - steam/routes/market.py
  - steam/routes/news.py
  - steam/services.py
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

## Mapa de degradaciones

Qué hace cada flujo cuando una fuente falla. **Decisión**: *conservar* (degradación
aceptada), *UX-46* (hacerla visible al usuario, necesita al front) o el issue del bug.
"Invisible" = el cliente recibe un 200 normal sin ninguna señal.

| Flujo | Disparador | Qué devuelve | ¿Lo ve el cliente? | Decisión |
|---|---|---|---|---|
| Inventario | 429 / 402 con snapshot | snapshot | `X-Inventory-Stale` | conservar (PERF-14) |
| Inventario | 402 sin snapshot | 503 | `code: upstream_quota` | conservar (SEC-16) |
| Inventario | 429 sin snapshot | 429, `detail` de texto | sin `code` | CAL-14 |
| Inventario | 410 / 411 | `[]` guardado en caché y snapshot | invisible | CAL-13 |
| Inventario | JSON inválido | 500 | 500 | CAL-14 |
| Perfil `/me` | 402 / 429 | 502 | sin `code` | CAL-14 |
| Perfil `/me` | campos ausentes | perfil en blanco cacheado 23 h | invisible | CAL-14 |
| Histórico (enriquecimiento) | fallo de csfloat/history | `[]` 5 min; deltas de `_inline_delta` | invisible | conservar |
| `/item/history` | ventana llena / 429 | stale, o 503 + `Retry-After` | `code: upstream_rate_limit` | conservar (SEC-16) |
| `/item/history` | 402 | `200 []` sin cachear | invisible | CAL-14 |
| `/item/history` | cuerpo no lista | `[]` cacheado 23 h | invisible | CAL-14 |
| Lookup CSFloat/Buff | fallo | stale o `{}`, backoff 5 min | precios a `null` | conservar (PERF-17) |
| Proveedores | fallo | stale o `_FALLBACK_PROVIDERS` | invisible | conservar (PERF-17) |
| FX | fallo / tasa fuera de 0,5–2,0 | última tasa o ninguna | `stale` en el cuerpo | conservar (UX-08) |
| Movers / trending | `/items` caído | fallback a topmovers, deltas `0.0` | invisible | UX-46 |
| Movers / trending | topmovers cacheado viejo | se usa sin mirar su edad | invisible | CAL-12 |
| `movers-tick` | ninguna fuente | conserva el snapshot anterior | `kept_previous` | conservar (CAL-10) |
| Trending | sticker slabs | no se filtran (sí en movers y búsqueda) | — | CAL-14 |
| Búsqueda / precio | 402 | stale, o 503 | `code: upstream_quota` | conservar (SEC-16) |
| Búsqueda / precio | 429 | 502 | sin `code` | CAL-14 |
| Búsqueda | caché compartida con el chat | hasta 10 items sin liquidez | invisible | CAL-11 |
| `/market/index` | 402 | stale, o 503 | `code: upstream_quota` | conservar (SEC-16) |
| `/market/index` | top sin `markethashname`/`change24h` | `KeyError` → 500 | 500 | CAL-14 |
| Catálogo de imágenes | todas las fuentes caídas | backoff 5 min, `image: ""` | invisible | conservar (CAL-08) |
| Noticias | JSON que no es dict | 500 | 500 | CAL-14 |
| Noticias | og:image falla | `imageUrl: ""` | invisible | conservar |
| Chat: precio / búsqueda | 402 / 429 | "error al ejecutar" | genérico | CAL-14 |
| Chat: inventario | cualquier error | `[]` | parece vacío | CAL-14 |
| Mappers | campos ausentes | `0`, `"Base Grade"`, `True`… | invisible | UX-46 |

El detalle por función (con número de línea aproximado) está en el primer comentario de
CAL-09 en Plane.
