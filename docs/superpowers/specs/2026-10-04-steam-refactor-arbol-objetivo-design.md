# Refactor `steam/` hacia el árbol objetivo — diagnóstico (Fase 0, CLEAN-13)

**Fecha:** 2026-10-04. **Prompt rector:** `docs/prompt-steam-refactor-agent.md`. **Plan maestro previo:** `docs/steam-module-refactor-plan.md`. **Plan de ejecución:** `docs/superpowers/plans/2026-10-04-steam-refactor-arbol-objetivo.md`.

## Resumen del módulo

3 992 líneas en 39 ficheros. La primera ronda del refactor (CAL-09, CLEAN-06..12) ya dejó: un cliente por fuente con errores tipados (`steam/clients/`, `steam/errors.py`), `Fetched[T]` y `log_degraded` (`steam/degraded.py`), `domain/{models,catalog,names,validators}.py`, mappers por dominio y 8 services. Línea base medida en este commit: **807 tests en verde, 16 `xfail(strict)`, cobertura 89 % (suelo 86), ruff 73, mypy 61**.

## Puntos críticos, impacto y prioridad

| # | Punto | Evidencia | Impacto | Prioridad |
|---|---|---|---|---|
| 1 | Sin adapters: los services leen el JSON crudo (`isinstance(data, list)` en 6 sitios, mapping de `HistoryPoint` duplicado en `services/pricing.py:66-77` y `:233-244`, providers/profile/news mapean inline) | `pricing.py`, `providers.py:51-69`, `profile.py:18-31`, `news.py:33`, `market.py:462-492` | Un cambio de forma en steamwebapi se detecta tarde y en sitios distintos; `KeyError` → 500 (`market.py:498`) | Alta (Fase 1) |
| 2 | Conversiones crudas `float(x or 0)` / `or 0` / `or ""` en 11 ficheros | `mappers/items.py:75-76,130-154`, `movers.py:26-27,62-65`, `market_index.py:8-10`, `rows.py:55-60,98-106`, `market.py:393,488-489`, `validators.py:41-42,50` | «No hay datos» y «cero» se confunden antes de llegar a quien decide (liquidez, rankings) | Alta (Fases 1-2) |
| 3 | 13 `except Exception`; `fetch_og_image` devuelve `""` sin log; `_topmovers` ignora `UpstreamError` con status; fallo parcial del catálogo sin `[steam-degraded]`; `capture_trending` purga aunque `compute` fallara | `clients/steam_news.py:38`, `market.py:202-213,667`, `catalog.py:150-159` | Degradaciones invisibles en producción | Alta (Fase 2) |
| 4 | 14 tests `xfail(strict=True)` de bugs abiertos CAL-11/12/13/14 | `tests/test_steam_contract_{items,market,ticks,tools}.py` | 500 por JSON inválido, 402/429 sin `code`, `[]` cacheado 23 h, perfil vacío cacheado, topmovers viejo, 410 pisa snapshot, caché del chat contamina búsqueda | Alta (Fase 2) |
| 5 | Caché sin capa: `TtlCache` en `stores.py` (raíz), 3 dicts planos (`_item_image_cache`, `_item_rarity_cache`, `_inventory_refresh_cooldown`), `stats()` sin lector, dos vocabularios de estado (`FetchStatus`/`Served`) | `stores.py:132-156`, `catalog.py:35,62,64` | Sin política explícita ni observabilidad; migrar a Redis (CAL-04) toca 17 instancias | Media (Fase 3) |
| 6 | Heurísticas por string: `is_sticker_slab` solo por nombre y no en trending; `weapon_category` por substring; color de noticia por substring de `feedname`; `skin_base` por `split(" (")` | `domain/names.py:53-59`, `domain/catalog.py:59-69`, `mappers/news.py:60-68`, `market.py:305-307` | Frágil ante nombres nuevos; comportamiento distinto entre movers y trending | Media (Fase 4) |
| 7 | Services que hacen demasiado: `services/market.py` 753 líneas; reintento 429 en `routes/items.py:79-177`; `price_capture.py` mezcla limiter + cliente + bucle + repo; `alerts/` importa el privado `_lookup_item` | — | Difícil de testear y de leer | Media (Fase 5) |
| 8 | Árbol distinto al objetivo (`clients/` vs `api/`, `errors.py` vs `errors/`, sin `utils/`, `liquidity.py` y `degraded.py` en la raíz) | `docs/features/steam.md` | Deuda de forma, no de comportamiento | Baja (se absorbe en 1, 3, 4, 6) |

## Riesgos del cambio (documentados antes de tocar código)

1. **Ratchet bidireccional** (`tools/ratchet.py`): cada `.py` nuevo/movido de `steam/` necesita test que lo importe por nombre completo + alta en `docs/features/steam.md` + `--bless`, en el mismo commit.
2. **~25 monkeypatch por string** en tests y **7 paquetes externos** que importan de `steam/` (`main`, `tools`, `alerts`, `predict`, `rag`, `notifications`, `auth`). Solo se tocan sus líneas de import.
3. **Contrato con el front**: `tests/test_steam_contract_*.py` no se editan salvo quitar `xfail`. El colapso `None → 0` en las tarjetas es contrato (`test_steam_contract_rows.py:76-78`): la distinción vive en el modelo interno.
4. **`liquidityBreakdown` nunca en `_row_to_item`** (`test_steam_contract_rows.py:81-85`).
5. **Entorno**: la verificación exige el venv con `requirements.txt` + `requirements-dev.txt`.

## Pruebas mínimas que se preservan

Las 807 actuales. Las de contrato (`test_steam_contract_*`) son el árbitro; `test_steam_layers.py` y `test_stores_ttl_cache.py` (ampliadas en CLEAN-13) y `test_domain_names.py::test_reglas_de_nombres_solo_en_domain` son las guardias arquitectónicas.

## Fuera de alcance

Añadir `weapontype`/`exterior`/`variants` a `_MOVERS_SELECT`; exponer `Fetched.status` al front (UX-46); mover los repos de Supabase; Redis (CAL-04).
