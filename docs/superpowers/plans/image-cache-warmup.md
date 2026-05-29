---
status: completed
---

# Plan: Precalentamiento de caché de imágenes en lifespan

## Problema

`_item_image_cache` se poblaba de forma lazy (primer request a `/market/movers` o
`/market/trending`). Los usuarios que llegaban antes de ese primer request veían
ítems sin imagen. El problema era reproducible: un usuario veía imágenes porque
llegaba después de que otro ya había calentado la caché; el primero no.

## Solución implementada

Llamada a `_fetch_static_images` en el lifespan de `main.py`, justo después de
crear el `httpx.AsyncClient`. La caché de imágenes de ByMykel (skins, knives,
stickers, keychains) se carga una vez al arrancar el servidor y queda disponible
para todos los requests desde el primer momento.

```python
# main.py — lifespan
app.state.http_client = httpx.AsyncClient(timeout=10.0)
await _fetch_static_images(app.state.http_client)  # ← añadido
yield
```

`_fetch_static_images` ya tiene su propio TTL (`IMAGE_CACHE_TTL = 82800 s`),
por lo que llamadas posteriores desde los endpoints de fallback son no-ops.

## Limitaciones conocidas

- La caché sigue siendo in-memory: se pierde en cada restart/deploy.
- No escala a múltiples workers (cada worker carga su propia copia).
- Si los JSONs de ByMykel no están disponibles al arrancar, el servidor arranca
  igualmente — `_fetch_static_images` captura la excepción y loguea un warning.

## Siguiente paso si se necesita escalar

Migrar `stores.py` a Redis (Upstash free tier en Render). Ver contexto en
`CLAUDE.md` sección "In-memory stores".
