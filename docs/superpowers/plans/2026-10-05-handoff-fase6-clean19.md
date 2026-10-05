# Handoff — Fase 6 (CLEAN-19) del refactor de `steam/`, tareas 6.2 y 6.3

> Prompt para la siguiente sesión. Estado al cierre de la sesión anterior (2026-10-05):
> Fases 0–5 cerradas y la tarea 6.1 de la Fase 6 hecha (commit `fe7607a`).

## Prompt

Continúa el refactor de `steam/` por la Fase 6 (CLEAN-19) siguiendo
`docs/superpowers/plans/2026-10-04-steam-refactor-arbol-objetivo.md`. Lee antes su sección
«Salida de fase» entera (Fases 0–5, en especial las «Pendiente para la Fase 6» de la 5) y
«Reglas comunes a todas las tareas». La tarea 6.1 (`steam/utils/{strings,dates,urls}.py`)
ya está hecha y marcada `— [x]`; quedan **6.2 y 6.3**.

Estado del repo:
- master contiene las Fases 0–5 y la 6.1 (último merge: la rama
  `claude/steam-refactor-fase4-apbdaq`, tip `fe7607a`). Trabaja en `claude/<nombre-de-sesión>`
  creada desde `origin/master`.
- DoD: crea el venv e instala `requirements.txt` y `requirements-dev.txt`;
  `venv/bin/python tools/dod.py` → «DOD: ALL 3 GATES GREEN» antes de cada commit. Baseline:
  ruff 71, mypy 56, coverage_floor 90 (1018 tests, cobertura 91 %). Fichero nuevo o movido:
  alta en `files:` de `docs/features/steam.md` + un test que lo importe. Si ruff/mypy bajan,
  `tools/ratchet.py --bless` en el mismo commit (A-8).
- El push desde la sesión cloud falla (403). Al cerrar: `git bundle create fase6-clean19.bundle
  origin/master..HEAD`, verificar, entregar con SendUserFile y borrarlo del árbol. Se integra en
  local con `merge --no-ff` a master.
- `tests/test_steam_contract_*.py` no se editan. Guardias: capas por AST en
  `tests/test_steam_layers.py` (incluye `utils`), prefijos de nombres en
  `tests/test_domain_normalizers.py`, `TtlCache` en `tests/test_cache_policy.py`.

### Tarea 6.2 — Comentarios, logs y `stores.py` (un commit, o dos si prefieres separar stores)

1. **Comentarios de diario fuera, invariantes dentro.** `grep -rnE "antes vivía|como antes|antes se |antes era|antes iba|ex [a-z_]+\.py|\(ex " steam --include=*.py` da ~27 líneas; quita
   las que solo cuentan historia («ex names.py», «antes el dict crudo», «CLEAN-xx lo mueve»)
   y conserva las que explican una invariante o una medición (`pricereal`, `_MOVERS_SELECT`
   con `prices`, PERF-14, CAL-13, el `.value` de los enums en 3.11, el troceado del
   price-tick…). Los docstrings de módulo dejan de citar CLEAN-xx cerrados: describen lo que
   el módulo es, no de dónde viene. Lo que sí tiene que quedar es la referencia a un issue
   cuando explica un porqué (SEC-16, PERF-09, CAL-08…).
2. **Prefijos de log por fuente.** Unificar a `[steam-client]`, `[catalog]`, `[item-history]`,
   `[market-items]`, `[market-movers]`, `[market-trending]`, `[cap-tick]`, `[trending-tick]`,
   `[enrich-tick]`, `[price]`, `[inventory]`, `[inventory-429]`, `[fx]`, `[news]`,
   `[providers]`. **No renombrar** `[inventory-429]` ni `[steam-degraded]` (los `grep`
   documentados en CLAUDE.md y docs/features/steam.md dependen de ellos) y comprobar que toda
   degradación tiene su `log_degraded` (mapa de `docs/features/steam.md`, columna «Log»;
   `tests/test_steam_degraded_logs.py` fija los flows).
3. **Retirar los alias de `stores.py`** (`_search_cache`, `_item_image_cache`,
   `_item_rarity_cache`, `_image_cache_meta`, `_profile_cache`, `_inventory_cache`, los TTL
   reexportados y el `TtlCache` compat; `grep -rn "_cache\b\|TtlCache\|_TTL" stores.py`).
   Hay 25 ficheros con `from stores import` (tests, tools, auth, main): cada uno pasa a
   importar de `steam.cache.<módulo>` (`history_cache`, `market_cache`, `user_cache`,
   `image_cache.catalog_cache`) o de `steam.cache.policy` para los TTL. Los stores de auth
   (`_nonces`, `_auth_codes`, `_rate_store`) y `_leetify_cache` se quedan en `stores.py`.
   Criterio de salida: `grep -rn "_cache" stores.py` → solo `_leetify_cache`;
   `tests/test_image_cache.py` adaptado a `catalog_cache` (hoy funciona vía los alias).
   Actualizar la sección «In-memory stores» de CLAUDE.md (apunta a `steam/cache/`) y la
   tabla de stores.

### Tarea 6.3 — Documentación y cierre (un commit)

- `CLAUDE.md`: «Module structure» y «Dependency order» con el árbol final (ya reflejan 4–6.1;
  revisar que ningún comentario del árbol cite «ex …» ni «Fase N»); «In-memory stores» →
  `steam/cache/`.
- `docs/features/steam.md`: mapa de degradaciones al día (toda fila con «Log» tiene test en
  `test_steam_degraded_logs.py`).
- `docs/steam-module-refactor-plan.md`: marcarlo como ejecutado con enlace al plan y al spec
  de 2026-10-04.
- `venv/bin/python tools/ratchet.py --bless` final solo si algo bajó; `coverage_floor` solo sube
  (hoy 91 % → puede pasar a 91 si el `TOTAL` lo da).
- Sección «Fase 6 (CLEAN-19)» al final del plan con *movido / igual / riesgo reducido /
  prueba / pendiente / desvíos*, las tareas con `— [x]`, y el **resumen de las 6 fases** que
  pide la 6.3 (una tabla: fase, issue, qué se movió, qué guardia lo protege).

Deuda consciente que **no** es de esta fase: `capture_trending` sigue purgando sin fuentes
(decisión del dueño del contrato, fila «`trending-tick` · ninguna fuente» del mapa);
`Fetched.status` no se expone al front (UX-46); migración a Redis (CAL-04).

## Qué hizo la sesión anterior (para contexto, no para repetir)

| Fase | Issue | Commits | Salida |
|---|---|---|---|
| 4 | CLEAN-17 | `664aa51`, `7ed9c0d`, `23f6736` | enums, `domain/{rules,normalizers,liquidity}.py`, `utils/urls.py`, slabs fuera del trending |
| 5 | CLEAN-18 | `884a8f3`, `748e7bf`, `d5036b8`, `a9bcd3c`, `b79d322` | `*_service.py`, `rankings_service` + `cap_history_service`, `inventory_service.get_inventory`, `price_capture_service.lookup_item` |
| 6.1 | CLEAN-19 | `fe7607a` | `utils/strings.py`, `utils/dates.py` |

Desvíos ya anotados en el plan (no reabrir): `is_sticker_slab` usa el nombre como respaldo
siempre que `item_type` no diga slab; `VALID_MARKETS = HISTORY_MARKETS | PASSTHROUGH_MARKETS`;
`_MOVERS_SELECT` vive en `rankings_service`; el cooldown del refresh sigue en la ruta;
`_fetch_fresh_inventory(client, steam_id)` se conserva como envoltorio para los tests del 429.
