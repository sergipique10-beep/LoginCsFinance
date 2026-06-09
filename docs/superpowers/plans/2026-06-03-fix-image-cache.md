# Fix Image Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Corregir tres bugs que impiden que las imágenes de las armas se carguen correctamente en el inventario y en los endpoints de mercado.

**Architecture:** Tres correcciones quirúrgicas en `steam/services.py` y `steam/routes/items.py` — sin nuevos archivos, sin cambios de interfaz. (1) Hacer que `_cache_images` indexe también por `market_hash_name` para cubrir el key del API que usa guión bajo. (2) Hacer que `_enrich_images_from_cache` sea idempotente en items ya enriquecidos (no resetea imágenes que ya tienen valor). (3) Hacer que `/inventory` llame `_fetch_static_images` y no guarde en caché ítems con imágenes vacías — o mejor dicho, que reintente el enriquecimiento antes de cachear.

**Tech Stack:** Python 3.12, FastAPI, httpx, in-memory dict cache (`_item_image_cache` en `stores.py`)

---

## Contexto de los bugs

### Bug 1 — `_cache_images` indexa por `markethashname` pero la API a veces usa `market_hash_name`
`_cache_images` en `services.py:110` itera sobre `(raw.get("markethashname"), raw.get("marketname"))`.
Si el raw viene de un endpoint que usa snake_case (`market_hash_name`), la imagen se pierde.
`_map_item` construye `name` como `d.get("marketname", "") or d.get("market_hash_name", "")` —
si `marketname` está vacío, `name` es el valor de `market_hash_name`, que **no está en el cache**.

### Bug 2 — `/inventory` guarda el resultado cacheado con `image = ""` antes de que el cache de imágenes esté caliente
Si el warmup en startup tardó o falló silenciosamente, el primer request de inventario llama
`_enrich_images_from_cache` con un cache vacío, guarda `image = ""` en `_inventory_cache`,
y durante las próximas 23 h todos los requests sirven ítems sin imagen.

### Bug 3 — `/inventory` nunca llama `_fetch_static_images`
Los endpoints de mercado llaman `_fetch_static_images` (que carga ByMykel) cuando usan el
fallback de topmovers. El inventario nunca lo llama, por lo que si el warmup falló al arrancar,
nunca hay segunda oportunidad de recuperar las imágenes estáticas.

---

## Ficheros modificados

| Fichero | Cambio |
|---------|--------|
| `steam/services.py` | Bug 1: añadir `market_hash_name` a las claves en `_cache_images` |
| `steam/routes/items.py` | Bug 2+3: llamar `_fetch_static_images` antes de `_enrich_images_from_cache`; no cachear ítems con imagen vacía si el cache de imágenes está disponible |

---

## Task 1: Fix `_cache_images` — indexar también por `market_hash_name`

**Files:**
- Modify: `steam/services.py:105-112`

- [ ] **Step 1: Localizar la función en el editor**

  Abrir `steam/services.py`. La función `_cache_images` empieza en la línea ~105:

  ```python
  def _cache_images(raw_items: list) -> None:
      for raw in raw_items:
          img = raw.get("image", "")
          if not img:
              continue
          for key in (raw.get("markethashname"), raw.get("marketname")):
              if key:
                  _item_image_cache[key] = img
  ```

- [ ] **Step 2: Aplicar la corrección**

  Reemplazar la función con la versión que incluye `market_hash_name`:

  ```python
  def _cache_images(raw_items: list) -> None:
      for raw in raw_items:
          img = raw.get("image", "")
          if not img:
              continue
          for key in (
              raw.get("markethashname"),
              raw.get("marketname"),
              raw.get("market_hash_name"),
          ):
              if key:
                  _item_image_cache[key] = img
  ```

- [ ] **Step 3: Verificar que el fichero es sintácticamente correcto**

  ```bash
  python -c "import ast; ast.parse(open('steam/services.py').read()); print('OK')"
  ```
  Expected: `OK`

- [ ] **Step 4: Commit**

  ```bash
  git add steam/services.py
  git commit -m "fix(image-cache): index by market_hash_name in _cache_images"
  ```

---

## Task 2: Fix `/inventory` — llamar `_fetch_static_images` y no cachear imágenes vacías

**Files:**
- Modify: `steam/routes/items.py:1-20` (imports) y `steam/routes/items.py:105-110` (handler)

- [ ] **Step 1: Añadir `_fetch_static_images` a los imports del fichero**

  Al principio de `steam/routes/items.py`, el bloque de imports de `..services` es:

  ```python
  from ..services import (
      STEAM_WEB_API,
      _enrich_prices,
      _enrich_market_prices,
      _enrich_images_from_cache,
  )
  ```

  Añadir `_fetch_static_images`:

  ```python
  from ..services import (
      STEAM_WEB_API,
      _enrich_prices,
      _enrich_market_prices,
      _enrich_images_from_cache,
      _fetch_static_images,
  )
  ```

- [ ] **Step 2: Actualizar el handler de `/inventory`**

  Localizar el bloque final del handler (líneas ~105-110):

  ```python
      items = [_map_item(item) for item in data]
      items = await _enrich_prices(request.app.state.http_client, items)
      items = await _enrich_market_prices(request.app.state.http_client, items)
      _enrich_images_from_cache(items)
      _inventory_cache[steam_id] = (items, now)
      return items
  ```

  Reemplazar con:

  ```python
      items = [_map_item(item) for item in data]
      items = await _enrich_prices(request.app.state.http_client, items)
      items = await _enrich_market_prices(request.app.state.http_client, items)
      await _fetch_static_images(request.app.state.http_client)
      _enrich_images_from_cache(items)
      _inventory_cache[steam_id] = (items, now)
      return items
  ```

  **Por qué este orden:** `_fetch_static_images` es idempotente (comprueba el TTL internamente y
  no hace nada si el cache ya está caliente). Llamarla justo antes de `_enrich_images_from_cache`
  garantiza que el cache de imágenes estará poblado cuando se intente enriquecer. El coste
  es cero si el cache ya existe; si no existía (startup falló), se repara aquí.

- [ ] **Step 3: Verificar sintaxis**

  ```bash
  python -c "import ast; ast.parse(open('steam/routes/items.py').read()); print('OK')"
  ```
  Expected: `OK`

- [ ] **Step 4: Smoke test — arrancar el servidor y comprobar que no hay errores de importación**

  ```bash
  python -c "from steam.routes.items import router; print('import OK')"
  ```
  Expected: `import OK`

- [ ] **Step 5: Commit**

  ```bash
  git add steam/routes/items.py
  git commit -m "fix(inventory): ensure image cache is warm before caching inventory results"
  ```

---

## Task 3: Verificar el fix end-to-end con logs

**Goal:** confirmar que los tres bugs están resueltos observando los logs del servidor.

- [ ] **Step 1: Arrancar el servidor en modo dev**

  ```bash
  python run_dev.py
  ```

  Observar los logs de startup. Deben aparecer líneas como:

  ```
  [image-cache] loaded XXXX total entries (+XXXX new) — sources: {'skins': ..., 'knives': ..., ...}
  ```

  Si aparece esta línea, el warmup en startup funcionó correctamente.

- [ ] **Step 2: Hacer una petición al inventario**

  Con el servidor corriendo, hacer un request a `/inventory` con un Bearer token válido
  (usar `POST /auth/dev-token` si `DEBUG=true` en `.env`).

  En los logs, verificar que **no** aparece:

  ```
  [image-cache] loaded 0 total entries
  ```

  Y que los items en la respuesta JSON tienen el campo `"image"` no vacío para los ítems
  que tienen skin (los ítems de tipo "sticker" pueden tener imagen vacía si no están en ByMykel).

- [ ] **Step 3: Verificar que el cache no persiste imágenes vacías tras el segundo request**

  Hacer un segundo request a `/inventory`. La respuesta debe venir desde caché
  (`_inventory_cache`) y las imágenes deben seguir presentes (no se han reseteado).

- [ ] **Step 4: Confirmar fix del Bug 1 manualmente**

  Revisar los logs en busca de la línea de search (si se usa `/market/items`):

  ```
  [market-items] q='...' → N results
  ```

  Verificar en la respuesta que los items devueltos tienen imagen. Si antes de este fix
  algunos items de `/market/items` tenían `image: ""`, ahora deben tener URL.

---

## Resumen de cambios

| Bug | Fichero | Líneas | Fix |
|-----|---------|--------|-----|
| 1 | `steam/services.py` | 110 | Añadir `market_hash_name` a las claves de `_cache_images` |
| 2+3 | `steam/routes/items.py` | 108 | Llamar `_fetch_static_images` antes de `_enrich_images_from_cache` en `/inventory` |

No se modifica la lógica de caché de inventario (TTL 23h) — el fix garantiza que cuando
se cachea, ya tiene imágenes. No se añaden nuevos endpoints ni estructuras de datos.
