# Liquidity Score — Diseño

**Fecha:** 2026-07-14
**Estado:** aprobado, pendiente de plan de implementación

## Problema

El frontend muestra `hoursToSold` crudo y una etiqueta `HOTTEST LIQUIDITY` en el Market, pero no existe ninguna medida de liquidez calculada. La idea original era replicar el porcentaje de liquidez de PriceEmpire (se observaron 66.31%, 42.55%, 54.65%, 48.90% en distintos ítems).

**Esa vía está cerrada.** PriceEmpire no publica su fórmula: documenta los factores (listings, gap de precios, volumen) pero no los pesos ni la normalización, y su sitio bloquea el acceso automatizado (HTTP 403). Sin los inputs crudos de cada ítem, ajustar pesos contra sus porcentajes de salida es sobreajuste, no ingeniería inversa. Además hay indicios de reglas ad-hoc por tipo de ítem (bonificaciones a Doppler FN) que ninguna fórmula limpia reproduce.

Construimos un score propio, explicable, sobre los campos que ya tenemos.

## Objetivo

Un `liquidityScore` de 0 a 100 que responda **una** pregunta: *si listo este ítem hoy, ¿en cuánto tiempo se vende y a qué precio real?*

Esa pregunta —velocidad de salida— es la que fija todos los pesos. Un score que midiera "salud del mercado" o "confianza en la valuación" tendría pesos distintos; no es este.

## Alcance

| Superficie | Score |
|---|---|
| `/inventory` | Sí — payload completo vía `_map_item` |
| `/market/items`, `/market/trending` | Sí — mismo mapper, mismos campos |
| Topmovers (`_map_topmovers_item`) | **No** — `None` |

Topmovers queda fuera porque su payload no trae `offerVolume`, `buyOrderVolume` ni `hoursToSold`; el mapper los pone en `0` duro. Calcular un score con ceros produciría un número inventado que diría "ilíquido" cuando la verdad es "no hay datos".

## Datos de entrada

Todos ya presentes en el payload de steamwebapi y mapeados en `_map_item` ([mappers.py:224-231](../../../steam/mappers.py)). **No se hace ninguna llamada extra a la API** — el limitador de 18 req/60s hace inviable cualquier enriquecimiento por ítem.

| Campo | Uso |
|---|---|
| `sold24h`, `sold7d` | Velocidad de ventas |
| `hoursToSold` | Tiempo de venta (estimación del proveedor) |
| `offerVolume` | Listings activos → cola derivada |
| `buyOrderVolume` | Demanda en espera |
| `buyOrderPrice` | Mejor bid → haircut de salida |
| `priceLatest` | Precio de vitrina → haircut de salida |
| `externalPrices` | Consistencia entre mercados |

## Fórmula

Cinco componentes, cada uno normalizado a `[0, 1]` y recortado a ese rango:

```
v = (sold24h + sold7d / 7) / 2

1. Velocidad          V = ln(1 + v) / ln(1 + 500)                      peso 0.30
2. Tiempo de venta    T = 1 − ln(1 + t) / ln(1 + 720)                  peso 0.25
                      donde t = hoursToSold si > 0
                            t = offerVolume / (sold24h / 24) si no
3. Haircut de salida  S = 1 − h / 0.50                                 peso 0.25
                      donde h = (priceLatest − buyOrderPrice) / priceLatest
4. Demanda en espera  D = ln(1 + buyOrderVolume) / ln(1 + 5000)        peso 0.10
5. Consistencia       C = min(externalPrices) / max(externalPrices)    peso 0.10

liquidityScore = 100 × Σ(peso_i × componente_i)
```

### Por qué estas anclas

- **500 ventas/día** (saturación de velocidad): por encima de ese volumen, más ventas ya no te hacen vender más rápido. Es el orden de magnitud de una case.
- **720 horas / 30 días** (piso de tiempo de venta): si tardás un mes, la liquidez es cero a efectos prácticos.
- **50% de haircut** (piso del spread): si el mejor bid está a la mitad del precio de vitrina, "vender rápido" significa regalar el ítem.
- **5000 buy orders** (saturación de demanda).

Las escalas logarítmicas son deliberadas: la diferencia entre 5 y 50 ventas/día importa muchísimo más que entre 400 y 450.

### Por qué estos pesos

Velocidad y tiempo de venta suman **0.55** — son la respuesta directa a "¿en cuánto se vende?". El haircut pesa **0.25** porque vender rápido a un precio malo no es liquidez, es una pérdida: un ítem cuyo mejor bid está 40% abajo del precio de vitrina no es líquido aunque se venda en una hora. Demanda y consistencia son señales de apoyo (0.10 cada una).

### Componente 2: proveedor primero, derivación como fallback

`hoursToSold` viene calculado por steamwebapi. Se usa cuando está presente. Cuando viene en `0` (ocurre), se cae a la **cola derivada**: `offerVolume / (sold24h / 24)` = cuántas horas tarda en agotarse el stock listado delante tuyo al ritmo de ventas actual. Es una magnitud con significado físico, no una proxy.

### Nota de diseño: la demanda suma, no resta

Una versión anterior de esta fórmula usaba un componente de "equilibrio del order book" (media geométrica sobre aritmética) que **penalizaba** el desbalance entre listings y buy orders. Bajo el objetivo *vender rápido* eso está invertido: una montaña de buy orders son compradores haciendo fila con la plata en la mano. El componente 4 premia el volumen de buy orders, no el equilibrio.

## Casos borde

### Datos faltantes → renormalización, y `None` si no alcanza

Si un componente no tiene datos (ej. `externalPrices` con un solo mercado → no hay consistencia que medir), se **descarta y los pesos se renormalizan** entre los componentes disponibles.

Si el peso disponible total es **< 0.5**, el score es `None`.

`None` renderiza como `"N/A"` en el frontend, respetando la convención que ya existe para `priceDelta24h/7d/30d`. Un `0%` falso es peor que un `N/A` honesto: diría "ítem ilíquido" cuando la verdad es "no sé".

### Basura de la API → descartar el componente

Si `buyOrderPrice > priceLatest × 1.05`, el bid está por encima del ask, lo cual es imposible en un mercado real. Se descarta el componente de haircut (en vez de premiar al ítem con un haircut negativo). Mismo espíritu que `_MAX_PLAUSIBLE_RATIO` en `_inline_delta`.

### Cero ventas ≠ sin datos

Si `sold24h` y `sold7d` son ambos `0` pero el ítem tiene el resto de los datos de mercado, el score da un valor bajo **legítimo**: el ítem no se mueve. No es `None`. Esta distinción entre *no sé* y *no se vende* es la que hay que preservar.

### El mismo ítem debe puntuar igual en toda la app

`_MOVERS_SELECT` ([routes/market.py:39](../../../steam/routes/market.py)) ya pide `sold24h`, `sold7d`, `offervolume`, `buyordervolume`, `buyorderprice` y `hourstosold` — los seis campos del score. **Pero no pide `prices`**, que es de donde sale `externalPrices`.

Consecuencia: sin tocar nada, el mismo ítem puntuaría distinto en el inventario (5 componentes, peso 1.00) que en el Market (4 componentes, peso 0.90 renormalizado). Un score que cambia según la pantalla en la que lo mirás es un bug, no una sutileza.

**Solución: agregar `prices` a `_MOVERS_SELECT`.** Un test debe guardar esa lista contra los campos que el score necesita, igual que `test_movers_select_pide_los_campos_que_map_item_necesita` ya hace para la familia `pricereal`. Es exactamente la misma clase de bug que produjo los badges "N/A" en todo el Market.

## Arquitectura

**Módulo nuevo `steam/liquidity.py`**, sin dependencias internas. Entra arriba de todo en el orden de dependencias documentado en CLAUDE.md, al mismo nivel que `steam/mappers.py`.

No va dentro de `mappers.py`: ese archivo ya tiene 388 líneas y está definido como "pure data transformers". El score es lógica de negocio con constantes propias y casos borde propios; aislado se testea sin tocar nada más.

Interfaz pública:

```python
def compute_liquidity(item: dict) -> tuple[float | None, dict | None]:
    """Devuelve (score 0-100 redondeado a 2 decimales | None, breakdown | None)."""
```

`_map_item` gana dos campos:

- `liquidityScore: float | None`
- `liquidityBreakdown: dict | None` — los cinco componentes con su valor y su peso efectivo, para que el panel de detalle muestre *por qué* dio ese número.

`_map_topmovers_item` fija ambos en `None`.

### Frontend

`interfaces.ts`: agregar `liquidityScore: number | null` y `liquidityBreakdown` al modelo del ítem. Los mocks (`inventory.mock.ts`, `market.mock.ts`) y los fixtures de los `.spec.ts` tienen literales completos del objeto — hay que actualizarlos o rompe el typecheck.

La UI (cómo se muestra el badge, si reemplaza a `hoursToSold`, colores por rango) queda **fuera de alcance de este spec**. Este spec entrega el número por la API; el diseño visual es un trabajo aparte.

## Testing

pytest 9.1.1 ya está instalado y `tests/test_price_deltas.py` establece el patrón: datos reales de la API como fixtures, docstrings que explican el *porqué* del caso. Se sigue ese patrón. (CLAUDE.md dice "There are no test or lint commands configured" — está desactualizado y hay que corregirlo.)

`tests/test_liquidity.py` cubre, con TDD (tests antes que implementación):

1. Un ítem líquido conocido (alto volumen, `hoursToSold` bajo, spread chico) puntúa alto.
2. Un ítem ilíquido (cero ventas, cola larga) puntúa bajo pero **no** `None`.
3. `externalPrices` con un solo mercado → componente descartado, pesos renormalizados, score válido.
4. Sin `buyOrderPrice` ni `hoursToSold` ni `offerVolume` → peso disponible < 0.5 → `None`.
5. `buyOrderPrice` > `priceLatest × 1.05` → haircut descartado, no un score inflado.
6. `_map_topmovers_item` → `liquidityScore is None`.
7. Cada componente recortado a `[0, 1]` (ej. `v` > 500 no produce V > 1).
8. `_MOVERS_SELECT` contiene los seis campos del score **más `prices`**, para que el mismo ítem puntúe igual en inventario y en Market.

## Qué queda explícitamente fuera

- Replicar el número de PriceEmpire. No es un objetivo; es imposible sin su fórmula.
- Calibración contra datos reales. Los pesos y las anclas son juicios razonados, no ajustes empíricos. Una vez en producción, correr el score sobre el inventario real y revisar si el *ranking* tiene sentido es el siguiente paso natural — y puede motivar ajustar constantes.
- La UI del badge.
- Persistencia o histórico del score.
