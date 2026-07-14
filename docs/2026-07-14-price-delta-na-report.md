# Todas las skins del inventario mostraban "N/A" en el price delta: informe

**Fecha:** 2026-07-14
**Rama:** `feat/real-pricedelta` (backend y frontend)
**Síntoma reportado:** el badge de variación de precio salía `N/A` en prácticamente todas las skins del inventario, mientras que otros trackers (Price Empire) sí mostraban cambios de precio para esas mismas skins.
**Resultado:** causa raíz encontrada y corregida. Los deltas del inventario ya muestran porcentajes reales.

---

## 1. Diagnóstico

El `N/A` no era aleatorio ni un fallo de datos puntuales: era **sistemático**, y se generaba en nuestro código.

Los deltas del inventario salen de `_inline_delta` (`steam/mappers.py`), que comparaba el precio actual (`pricelatestsell`) contra los campos históricos de steamwebapi `pricelatestsell24h / 7d / 30d`. Cuando el histórico coincidía con el actual, devolvía `None` → el frontend pinta `"N/A"`.

El problema: **esos cuatro campos vienen siempre con el mismo valor**. No son precios históricos, son copias del precio actual.

### Evidencia

Llamada real a steamwebapi pidiendo las mismas Desert Eagle | Mulberry de la captura de Price Empire:

```
Desert Eagle | Mulberry (Factory New)
   pricelatestsell     = 23.7199993
   pricelatestsell24h  = 23.7199993   ← idéntico
   pricelatestsell7d   = 23.7199993   ← idéntico
   pricelatestsell30d  = 23.7199993   ← idéntico
```

Igual en las cinco variantes de desgaste. Comparar el precio consigo mismo da `None` siempre, para todo ítem con ventas. De ahí que el `N/A` fuera universal.

### Por qué el inventario y no el resto de la app

Market, trending y movers llaman a `_enrich_prices`, que calcula los deltas contra el histórico real de `csfloat/history` y **sobrescribe** los valores del mapper. El inventario no la llama: se eliminó en el commit `aff055f` (*"fixed price delta hot and cold"*, 29-jun), y con razón — implicaría una llamada a la API por cada ítem del inventario, y el limitador está en 18/60 s.

Al quedarse sin esa red de seguridad, el inventario pasó a depender **exclusivamente** de `_inline_delta`, que estaba leyendo campos inútiles. Las demás secciones enmascaraban el bug; el inventario lo dejó a la vista.

### El dato sí existía

En la misma respuesta hay otra familia de campos, `pricereal*`, que **sí varía por timeframe** y cuadra con lo que muestra Price Empire:

| Skin | `pricereal` (nosotros) | Price Empire | Δ7d nuestro | Δ7d Price Empire |
|---|---|---|---|---|
| Deagle Mulberry (FN) | 17.57 | $17.58 | −6.09% | −5.69% |
| Deagle Mulberry (MW) | 8.53 | $8.53 | 0.00% | −0.70% |

(`priceavg*` se descartó: también viene plano.)

---

## 2. El fix

### Backend — `steam/mappers.py`

`_inline_delta` pasa a comparar `pricereal` contra `pricereal24h / 7d / 30d`:

```python
"priceDelta24h":  _inline_delta(real, d.get("pricereal24h")),
"priceDelta7d":   _inline_delta(real, d.get("pricereal7d")),
"priceDelta30d":  _inline_delta(real, d.get("pricereal30d")),
```

Se añadió además un **guardarraíl de cordura**: se descarta cualquier precio histórico que esté a más de 10× de distancia del actual. No es teórico — la API devolvió `pricereal30d = 0.22` para una skin de $17.57, lo que habría pintado un **+7886%** falso.

### Frontend — `inventory.html`

El badge de las cards pasa de `priceDelta30d` a `priceDelta7d`, alineado con Market y Home, que ya usaban 7d. El bloque "Top Performer" se queda en 30d porque su etiqueta dice literalmente "Last 30 Days".

### Decisión de diseño: se mantiene `N/A`, no se rellena con `0%`

Se valoró usar `0%` como fallback cuando no hay dato. **Se descartó**: `0%` significa "el precio no se ha movido" y `N/A` significa "no sabemos si se ha movido". Colapsarlos hace imposible distinguir una skin genuinamente estable de una sin datos, y equivale a inventarse el dato.

Es además lo que hizo visible este bug: si el fallback hubiera sido `0%`, el inventario llevaría dos semanas mostrando "todo plano, todo en orden" y nadie se habría enterado de que los deltas estaban rotos. El `N/A` es el detector de humos.

---

## 3. Verificación

- **Tests nuevos:** `tests/test_price_deltas.py` (3 casos, escritos antes del fix). Fallaban con `assert None == -2.39`, es decir, reproducían el `N/A` exacto sobre el payload real de la API.
- **Suites completas:** 26 backend + 15 frontend, todas en verde.
- **End-to-end contra steamwebapi en vivo**, pasando la respuesta por `_map_item`:

```
SKIN                                        24H        7D       30D
Desert Eagle | Mulberry (Battle-Scarred)  -4.35%   -12.87%   -24.14%
Desert Eagle | Mulberry (Well-Worn)       -1.36%    -6.84%    -0.91%
Desert Eagle | Mulberry (Field-Tested)    +2.20%    -1.28%    -6.45%
Desert Eagle | Mulberry (Minimal Wear)    -0.12%    +0.00%    -0.23%
Desert Eagle | Mulberry (Factory New)     -2.39%    -6.09%       N/A
```

Los `N/A` restantes son los correctos: el 30d de la FN es el valor corrupto que descarta el guardarraíl, y los Souvenir sin ventas no tienen histórico que comparar.

---

## 4. Documentación actualizada

Ambos `CLAUDE.md` estaban desactualizados, y eso contribuyó a que el bug pasara desapercibido:

- El del **backend** afirmaba que `/inventory` enriquecía los precios con `_enrich_prices`. Dejó de ser cierto en junio. Ahora documenta que el inventario depende solo de `_inline_delta`, y advierte explícitamente de no usar los campos `pricelatestsell*`.
- El del **frontend** decía que los deltas se calculan vía `/history` y que el inventario usaba 30d. Ahora refleja `pricereal*` y el badge a 7d.

---

## 5. Pendiente

Los cambios están **sin commitear**, en la rama `feat/real-pricedelta` de los dos repos. Hasta que se despliegue el backend, la app en producción y en Android seguirá mostrando `N/A`.
