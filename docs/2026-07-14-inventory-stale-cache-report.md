# El inventario no se actualizaba: informe

**Fecha:** 2026-07-14
**Síntoma reportado:** skins conseguidas hacía días no aparecían en la app. El botón de refresh manual giraba, no daba error, y no cambiaba nada.
**Resultado:** una causa encontrada y corregida (caché de steamwebapi). Una segunda causa identificada y **fuera de nuestro alcance** (Steam oculta ítems de intercambios recientes).

---

## 1. Diagnóstico

Se recorrió la cadena capa por capa, comparando cada eslabón contra el siguiente, en vez de parchear a ciegas. Las tres primeras capas quedaron descartadas con evidencia:

| Capa | Comprobación | Veredicto |
|------|--------------|-----------|
| Frontend (TanStack Query, WebView) | Sin service worker, sin persistencia, sin `Cache-Control` en las respuestas | Descartada |
| `_map_item` (backend) | 199 ítems entran → 199 salen, no descarta ninguno | Descartada |
| `_inventory_cache` (backend, 23 h) | `POST /inventory/refresh` ya lo saltaba correctamente | Descartada |
| **steamwebapi** | **Servía su propia copia cacheada** | **Causa raíz** |

### El fallo

steamwebapi mantiene **su propia instantánea** del inventario y la sirve por defecto. Es una capa de caché **independiente** de la nuestra, y estaba congelada desde antes de que el usuario consiguiera las skins.

Nuestro `_fetch_fresh_inventory` nunca le pasaba el parámetro `no_cache`, que es el que documentan para forzar una lectura viva contra Steam. El resultado era un refresh que solo era forzado a medias:

```
POST /inventory/refresh
  → salta _inventory_cache (nuestro, 23 h)   ✅
  → pide a steamwebapi ... sin no_cache      ❌
  → steamwebapi responde desde SU caché      ← datos viejos
```

Eso explica el síntoma exacto: el botón llegaba al servidor y la petición se completaba con éxito, pero devolvía la misma lista de siempre. De ahí que girase y no diera error.

También explica por qué no se arreglaba solo con el paso de los días: el `GET /inventory` pasivo, al expirar su TTL de 23 h, chocaba contra la misma pared.

### Evidencia

Llamadas directas a steamwebapi con la cuenta real, mismo momento, único cambio el parámetro:

| Llamada | Ítems | Skins nuevas |
|---------|-------|--------------|
| `/inventory` **sin** `no_cache` (lo que hacía el backend) | **133** | **ninguna** |
| `/inventory` **con** `no_cache=1` | **141** | **todas** |

Las 8 skins recuperadas:

- Dual Berettas \| Rose Nacre (MW)
- Kilowatt Case
- Desert Eagle \| Mulberry (FN)
- MAC-10 \| Curse (FN)
- StatTrak™ FAMAS \| Mecha Industries (FN)
- Five-SeveN \| Hot Shot (MW)
- ★ Specialist Gloves \| Blackbook (FT)
- StatTrak™ UMP-45 \| Roadblock (FT)

---

## 2. El fix

Una línea en `_fetch_fresh_inventory` (`steam/routes/items.py`), que es la función **compartida** por `GET /inventory` y `POST /inventory/refresh`. Arreglarla ahí corrige ambos caminos: el botón fuerza una lectura real, y el refresco automático diario también.

```python
params={
    "steam_id": steam_id,
    "game": STEAM_GAME,
    "key": STEAM_API_KEY,
    "language": "english",
    "limit": 5000,
    "no_cache": 1,   # ← nuevo
}
```

**No aumenta el consumo de cuota.** Nuestro caché de 23 h sigue limitando a ~1 llamada al día por usuario; simplemente esa llamada ahora trae datos vivos. Es una consideración real dado el plan Starter de steamwebapi (20 req/60 s por endpoint, 2 000/día).

### Verificación

- **Test nuevo:** `tests/test_inventory_no_cache.py`, que cubre los dos caminos (`GET` y `POST /refresh`). Falla con `KeyError: 'no_cache'` sin el fix.
- **Suite completa:** 23 passed.
- **End-to-end contra steamwebapi en vivo:** `GET /inventory` devuelve 141 ítems con las 8 skins nuevas dentro (antes 133, sin ninguna).
- **Contra el endpoint real del botón:** `POST /inventory/refresh` en el backend local devuelve las 141 con las 8 nuevas.

---

## 3. Lo que este fix NO arregla

El usuario tiene además **2 cuchillos y unos guantes** que siguen sin aparecer. **No es un bug nuestro y no tiene solución desde nuestro código.**

Steam declara `total_inventory_count: 184` pero **solo entrega 166 assets** a cualquier petición sin la sesión del dueño. Entre los 18 que se guarda están esos ítems, todos procedentes de un intercambio reciente. Verificado desde tres orígenes distintos que coinciden: el endpoint público de Steam, steamwebapi con `no_cache=1`, y la respuesta cruda de Steam vía `parse=0` (leída desde las IPs de steamwebapi, sin nuestro rate limit).

**Confirmado por el propio usuario**: en una ventana de incógnito, él tampoco ve esos ítems en su inventario de Steam. El inventario público es la única fuente que tiene cualquier API — la nuestra y la de cualquier otro.

La única vía técnica sería el parámetro `steam_login_secure` de steamwebapi (documentado como *"fetching your own inventory without the 10-day trade block"*). **Se descartó**, por buenas razones:

- Esa cookie **es la sesión de Steam del usuario**: quien la tenga puede aceptar intercambios y vender en el mercado en su nombre.
- Habría que enviársela a **steamwebapi, un tercero**, en cada petición.
- Caduca cada pocos días y habría que renovarla a mano.
- **No escala**: el login por OpenID de la app nunca entrega esa cookie, así que para el resto de usuarios el problema seguiría igual. Es un parche personal, no una solución de producto.

Cuando Steam publique esos ítems, aparecerán solos.

### Nota aparte: ítems no comercializables

steamwebapi devuelve **solo ítems tradables**. De los 166 assets públicos, 141 son `tradable=1`, y steamwebapi devuelve exactamente esos 141. Medallas, monedas de operación, graffitis, music kits y charm packs no aparecen nunca. **Se decidió dejarlo así**: sin precio de mercado no aportan al valor del portfolio, que es de lo que va la app.

---

## 4. Pendiente

El fix está **solo en local**. Hasta que se despliegue, la app en Android y en producción seguirá mostrando 133 ítems.
