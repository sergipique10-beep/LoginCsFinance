# Liquidity Score Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Calcular un `liquidityScore` de 0 a 100 que responda "si listo este ítem hoy, ¿en cuánto se vende y a qué precio real?", derivado de campos que steamwebapi ya devuelve.

**Architecture:** Un módulo puro nuevo (`steam/liquidity.py`) sin dependencias internas, con cinco componentes ponderados y renormalización de pesos cuando faltan datos. `steam/mappers.py` lo llama al final de `_map_item`. El score no dispara ninguna llamada extra a la API — el limitador de 18 req/60s lo haría inviable.

**Tech Stack:** Python 3 / FastAPI, pytest 9.1.1 (ya instalado). Frontend Angular 20 / Ionic (repo aparte: `CS-FINANCE-ionic`).

**Spec:** `docs/superpowers/specs/2026-07-14-liquidity-score-design.md`

## Global Constraints

- **Sin llamadas extra a la API.** El score sale del payload que ya llega. `steam/services.py` no se toca.
- **`None` significa "no sé", `0` significa "no se mueve".** Nunca devolver `0` por datos faltantes. `None` renderiza `"N/A"` en el frontend, igual que `priceDelta24h`.
- **`liquidity.py` no importa nada interno.** Entra al mismo nivel que `mappers.py` en el orden de dependencias de CLAUDE.md.
- **Constantes exactas del spec:** saturación de velocidad `500.0` ventas/día; piso de tiempo `720.0` horas; haircut máximo `0.50`; saturación de buy orders `5000.0`; bid máximo sobre ask `1.05`; cobertura mínima de peso `0.5`.
- **Pesos exactos del spec:** velocidad `0.30`, tiempo de venta `0.25`, haircut `0.25`, demanda `0.10`, consistencia `0.10`.
- **Los tests siguen el patrón de `tests/test_price_deltas.py`:** docstrings que explican el *porqué* del caso, datos con forma real de la API.
- Todos los comandos se corren desde `LoginCsfinance/` salvo la Task 4, que corre desde `CS-FINANCE-ionic/`.

---

## File Structure

| Archivo | Responsabilidad |
|---|---|
| `steam/liquidity.py` (**crear**) | Los cinco componentes, los pesos, la renormalización y el score. Puro, sin I/O. |
| `tests/test_liquidity.py` (**crear**) | Casos felices, casos borde, guardia del `select`. |
| `steam/mappers.py` (**modificar**) | `_map_item` llama a `compute_liquidity`; `_map_topmovers_item` fija `None`. |
| `steam/routes/market.py` (**modificar**) | Agregar `prices` a `_MOVERS_SELECT`. |
| `CLAUDE.md` (**modificar**) | Documentar el módulo; corregir la línea que dice que no hay tests. |
| `CS-FINANCE-ionic/src/app/models/interfaces.ts` (**modificar**) | Contrato `liquidityScore` / `liquidityBreakdown`. |
| `CS-FINANCE-ionic/src/app/features/*/data/*.mock.ts`, `*.spec.ts` (**modificar**) | Fixtures: sin esto no compila el typecheck. |

**Nota de interfaz importante:** `compute_liquidity` recibe el dict **crudo de steamwebapi** (claves en minúscula: `sold24h`, `offervolume`, `hourstosold`...), **no** el dict ya mapeado. Motivo: `_map_item` colapsa `None` a `0` (`d.get("sold24h") or 0`), lo que destruiría la distinción entre "no hay datos" y "cero ventas" — justamente la que el spec exige preservar.

---

### Task 1: Módulo `steam/liquidity.py` — los cinco componentes y el score

**Files:**
- Create: `steam/liquidity.py`
- Test: `tests/test_liquidity.py`

**Interfaces:**
- Consumes: nada.
- Produces: `compute_liquidity(raw: dict) -> tuple[float | None, dict | None]` — devuelve `(score 0-100 redondeado a 2 decimales, breakdown)` o `(None, None)`. El `breakdown` es `{nombre_componente: {"value": float, "weight": float}}` donde `weight` es el peso **ya renormalizado** entre los componentes disponibles.

- [ ] **Step 1: Escribir el test que falla — un ítem líquido puntúa alto**

Crear `tests/test_liquidity.py`:

```python
"""El Liquidity Score responde: si listo este ítem hoy, ¿en cuánto se vende y a qué precio real?

Todos los pesos salen de esa pregunta. Un score que midiera "salud del mercado"
tendría otros. Ver docs/superpowers/specs/2026-07-14-liquidity-score-design.md.
"""
from steam.liquidity import compute_liquidity


# Ítem de alta rotación: se vende mucho, hay una montaña de compradores esperando,
# pero los mercados no coinciden en el precio (Steam $1.50 vs CSFloat $0.88).
# Sin `buyorderprice` ni `hourstosold` — el componente de haircut se descarta y el
# tiempo de venta se deriva de la cola de listings.
ITEM_LIQUIDO = {
    "markethashname": "AK-47 | Redline (Field-Tested)",
    "pricelatestsell": 1.50,
    "sold24h": 131,
    "sold7d": 894,
    "offervolume": 185,
    "buyordervolume": 6297,
    "prices": [
        {"market": "steam",   "price": 1.50, "quantity": 185},
        {"market": "buff",    "price": 0.96, "quantity": 40},
        {"market": "csfloat", "price": 0.88, "quantity": 12},
    ],
}


def test_item_liquido_puntua_alto():
    """131 ventas/día y 6297 buy orders: se vende solo.

    Lo que lo frena es el spread del 41% entre Steam y CSFloat (consistencia 0.59)
    y una cola de 185 listings ≈ 34h por delante.
    """
    score, breakdown = compute_liquidity(ITEM_LIQUIDO)

    assert score == 67.83
    assert breakdown["velocity"]["value"] == 0.7834
    assert breakdown["demand"]["value"] == 1.0        # 6297 buy orders satura
    assert breakdown["consistency"]["value"] == 0.5867
    assert "haircut" not in breakdown                 # sin buyorderprice → descartado
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `python -m pytest tests/test_liquidity.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'steam.liquidity'`

- [ ] **Step 3: Escribir `steam/liquidity.py`**

```python
"""Liquidity Score: qué tan rápido podés convertir un ítem en dinero.

Responde UNA pregunta: si listo este ítem hoy, ¿en cuánto se vende y a qué precio
real? Todos los pesos salen de ahí — un score que midiera "salud del mercado" o
"confianza en la valuación" tendría otros.

Recibe el dict CRUDO de steamwebapi, no el ya mapeado: `_map_item` colapsa None a 0
(`d.get("sold24h") or 0`) y eso destruiría la distinción entre "no hay datos" (None,
→ "N/A") y "cero ventas" (0, → score bajo legítimo).

Ver docs/superpowers/specs/2026-07-14-liquidity-score-design.md.
"""
import math

# Anclas de saturación. La justificación de cada número está en el spec.
_VELOCITY_SATURATION = 500.0   # ventas/día: más volumen que esto ya no acelera la venta
_HOURS_FLOOR = 720.0           # 30 días: tardar un mes es liquidez cero a efectos prácticos
_MAX_HAIRCUT = 0.50            # si el bid está al 50% de la vitrina, "vender rápido" es regalar
_BUYORDER_SATURATION = 5000.0

# Un bid por encima del ask es imposible en un mercado real: es basura de la API.
# Mismo espíritu que _MAX_PLAUSIBLE_RATIO en mappers._inline_delta.
_MAX_BID_OVER_ASK = 1.05

# Si los componentes disponibles no cubren al menos esta fracción del peso total,
# no sabemos lo suficiente para dar un número. Preferimos "N/A" a un 0% falso.
_MIN_WEIGHT_COVERAGE = 0.5

_WEIGHTS = {
    "velocity":    0.30,   # se vende mucho
    "timeToSell":  0.25,   # se vende rápido
    "haircut":     0.25,   # te pagan cerca del precio de vitrina
    "demand":      0.10,   # hay compradores esperando
    "consistency": 0.10,   # los mercados coinciden en el precio
}


def _num(value) -> float | None:
    """None si el valor falta o no es numérico. NO colapsa 0 a None: 0 es un dato."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _log_ratio(value: float, saturation: float) -> float:
    """Escala logarítmica: la diferencia entre 5 y 50 importa mucho más que entre 400 y 450."""
    return _clamp(math.log1p(value) / math.log1p(saturation))


def _velocity(raw: dict) -> float | None:
    sold24h = _num(raw.get("sold24h"))
    sold7d = _num(raw.get("sold7d"))
    if sold24h is None and sold7d is None:
        return None
    v = ((sold24h or 0.0) + (sold7d or 0.0) / 7.0) / 2.0
    return _log_ratio(v, _VELOCITY_SATURATION)


def _time_to_sell(raw: dict) -> float | None:
    """Horas hasta vender. Usa la estimación del proveedor; si viene en 0, deriva la cola.

    La cola = cuántas horas tarda en agotarse el stock listado delante tuyo al ritmo
    de ventas actual. Es una magnitud con significado físico, no una proxy.
    """
    hours = _num(raw.get("hourstosold"))
    if not hours:
        listings = _num(raw.get("offervolume"))
        sold24h = _num(raw.get("sold24h"))
        if listings is None or sold24h is None:
            return None
        if sold24h <= 0:
            return 0.0   # nada se vendió en 24h: la cola no avanza. No es "no sé", es "no se mueve".
        hours = listings / (sold24h / 24.0)
    return _clamp(1.0 - math.log1p(hours) / math.log1p(_HOURS_FLOOR))


def _haircut(raw: dict) -> float | None:
    """Cuánto perdés si querés salir ya: la distancia entre el mejor bid y la vitrina."""
    price = _num(raw.get("pricelatestsell")) or _num(raw.get("price"))
    bid = _num(raw.get("buyorderprice"))
    if not price or not bid:
        return None
    if bid > price * _MAX_BID_OVER_ASK:
        return None   # bid por encima del ask: imposible. Basura de la API, no un chollo.
    return _clamp(1.0 - ((price - bid) / price) / _MAX_HAIRCUT)


def _demand(raw: dict) -> float | None:
    """Buy orders = compradores haciendo fila con la plata en la mano. Suma, no resta."""
    buy_orders = _num(raw.get("buyordervolume"))
    if buy_orders is None:
        return None
    return _log_ratio(buy_orders, _BUYORDER_SATURATION)


def _consistency(raw: dict) -> float | None:
    """Si los mercados divergen, tu 'precio' depende de dónde vendas."""
    prices = [_num(p.get("price")) for p in (raw.get("prices") or [])]
    prices = [p for p in prices if p and p > 0]
    if len(prices) < 2:
        return None
    return _clamp(min(prices) / max(prices))


def compute_liquidity(raw: dict) -> tuple[float | None, dict | None]:
    """Score 0-100 + desglose. (None, None) cuando no hay datos suficientes."""
    components = {
        "velocity":    _velocity(raw),
        "timeToSell":  _time_to_sell(raw),
        "haircut":     _haircut(raw),
        "demand":      _demand(raw),
        "consistency": _consistency(raw),
    }
    available = {k: v for k, v in components.items() if v is not None}
    total_weight = sum(_WEIGHTS[k] for k in available)
    if total_weight < _MIN_WEIGHT_COVERAGE:
        return None, None

    score = sum(_WEIGHTS[k] * v for k, v in available.items()) / total_weight * 100.0
    breakdown = {
        k: {"value": round(v, 4), "weight": round(_WEIGHTS[k] / total_weight, 4)}
        for k, v in available.items()
    }
    return round(score, 2), breakdown
```

- [ ] **Step 4: Correr el test para verificar que pasa**

Run: `python -m pytest tests/test_liquidity.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Agregar el test de recorte a `[0, 1]`**

Agregar a `tests/test_liquidity.py`:

```python
def test_componentes_recortados_a_cero_uno():
    """Una case con 5000 ventas/día no puede dar velocity > 1 y romper la escala 0-100."""
    score, breakdown = compute_liquidity({
        **ITEM_LIQUIDO,
        "sold24h": 5000,
        "sold7d": 35000,
        "buyordervolume": 99999,
    })

    assert breakdown["velocity"]["value"] == 1.0
    assert breakdown["demand"]["value"] == 1.0
    assert 0.0 <= score <= 100.0
```

- [ ] **Step 6: Correr los tests**

Run: `python -m pytest tests/test_liquidity.py -v`
Expected: PASS (2 passed)

- [ ] **Step 7: Commit**

```bash
git add steam/liquidity.py tests/test_liquidity.py
git commit -m "feat: Liquidity Score — módulo de cálculo

Cinco componentes ponderados (velocidad, tiempo de venta, haircut de salida,
demanda en espera, consistencia entre mercados) sobre campos que steamwebapi
ya devuelve. Sin llamadas extra a la API.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Casos borde — renormalización, `None` honesto, basura de la API

**Files:**
- Test: `tests/test_liquidity.py` (agregar)
- Modify: `steam/liquidity.py` (solo si algún test falla)

**Interfaces:**
- Consumes: `compute_liquidity` de la Task 1.
- Produces: nada nuevo. Esta task verifica que la semántica `None` vs `0` de la Task 1 es correcta.

Los tres casos de esta task son la razón por la que el score se testea en vez de mirarse a ojo: un `0%` falso y un `0%` legítimo se ven idénticos en la UI.

- [ ] **Step 1: Escribir los tests borde**

Agregar a `tests/test_liquidity.py`:

```python
def test_item_sin_ventas_puntua_bajo_pero_no_es_none():
    """Cero ventas NO es "sin datos": es "no se mueve". Un None acá sería mentir.

    El único componente que sobrevive es el haircut (bid a $700 de una vitrina de
    $1000 → perdés 30% si salís ya). Los pesos se renormalizan sobre 0.90 porque
    no hay `prices` con dos mercados.
    """
    score, breakdown = compute_liquidity({
        "markethashname": "★ Kukri Knife | Stained (Factory New)",
        "pricelatestsell": 1000.0,
        "buyorderprice": 700.0,
        "sold24h": 0,
        "sold7d": 0,
        "offervolume": 40,
        "buyordervolume": 0,
        "prices": [{"market": "steam", "price": 1000.0, "quantity": 40}],
    })

    assert score == 11.11          # (0.25 × 0.4) / 0.90 × 100
    assert score is not None
    assert breakdown["velocity"]["value"] == 0.0
    assert breakdown["timeToSell"]["value"] == 0.0
    assert breakdown["haircut"]["value"] == 0.4
    assert "consistency" not in breakdown   # un solo mercado → nada que comparar


def test_pesos_se_renormalizan_entre_componentes_disponibles():
    """Sin `prices`, los 4 componentes restantes deben sumar peso 1.0, no 0.90.

    Si no renormalizáramos, todo ítem sin datos de mercados externos tendría un techo
    del 90% — un castigo por un dato que falta, no por ser ilíquido.
    """
    _, breakdown = compute_liquidity({
        "pricelatestsell": 10.0,
        "buyorderprice": 9.0,
        "sold24h": 50,
        "sold7d": 350,
        "offervolume": 100,
        "buyordervolume": 200,
    })

    assert "consistency" not in breakdown
    assert round(sum(c["weight"] for c in breakdown.values()), 4) == 1.0


def test_sin_datos_suficientes_devuelve_none():
    """Menos de la mitad del peso disponible → "N/A", no un 0% que diría "ilíquido"."""
    score, breakdown = compute_liquidity({
        "markethashname": "Souvenir AWP | Dragon Lore (Factory New)",
        "pricelatestsell": 15000.0,
    })

    assert score is None
    assert breakdown is None


def test_bid_por_encima_del_ask_descarta_el_haircut():
    """buyorderprice > pricelatestsell × 1.05 es imposible: basura de la API.

    Sin la guardia, el haircut daría negativo → se recortaría a 1.0 → el ítem
    cobraría 0.25 de peso completo por un dato corrupto.
    """
    basura = {
        "pricelatestsell": 100.0,
        "buyorderprice": 130.0,     # bid 30% por encima del ask
        "sold24h": 10,
        "sold7d": 70,
        "offervolume": 50,
        "buyordervolume": 100,
    }
    sano = {**basura, "buyorderprice": 95.0}

    _, breakdown_basura = compute_liquidity(basura)
    _, breakdown_sano = compute_liquidity(sano)

    assert "haircut" not in breakdown_basura
    assert breakdown_sano["haircut"]["value"] == 0.9   # 1 − (0.05 / 0.50)
```

- [ ] **Step 2: Correr los tests**

Run: `python -m pytest tests/test_liquidity.py -v`
Expected: PASS (6 passed). Si alguno falla, corregir `steam/liquidity.py` — no el test — y volver a correr.

- [ ] **Step 3: Commit**

```bash
git add tests/test_liquidity.py steam/liquidity.py
git commit -m "test: casos borde del Liquidity Score

None significa 'no sé' y 0 significa 'no se mueve'. Un 0% falso y un 0%
legítimo se ven idénticos en la UI: por eso esto se testea.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Integración — mappers, `_MOVERS_SELECT` y docs

**Files:**
- Modify: `steam/mappers.py` (import + `_map_item` + `_map_topmovers_item`)
- Modify: `steam/routes/market.py:39-55` (`_MOVERS_SELECT`)
- Modify: `CLAUDE.md`
- Test: `tests/test_liquidity.py` (agregar)

**Interfaces:**
- Consumes: `compute_liquidity(raw) -> tuple[float | None, dict | None]` de la Task 1.
- Produces: los ítems mapeados ganan `liquidityScore: float | None` y `liquidityBreakdown: dict | None`. El frontend (Task 4) consume esos dos nombres exactos.

El bug que esta task previene: `_MOVERS_SELECT` pide campos explícitos. Si `prices` no está en la lista, la API no lo devuelve, el componente de consistencia se descarta **solo en el Market**, y el mismo ítem puntúa distinto según la pantalla. Es la misma clase de bug que produjo los badges `"N/A"` en todo el Market (ver `test_movers_select_pide_los_campos_que_map_item_necesita`).

- [ ] **Step 1: Escribir los tests de integración que fallan**

Agregar a `tests/test_liquidity.py`:

```python
from steam.mappers import _map_item, _map_topmovers_item
from steam.routes.market import _MOVERS_SELECT


def test_map_item_expone_el_score_y_el_desglose():
    item = _map_item(ITEM_LIQUIDO)

    assert item["liquidityScore"] == 67.83
    assert item["liquidityBreakdown"]["velocity"]["value"] == 0.7834


def test_topmovers_no_inventa_un_score():
    """El payload de topmovers no trae offervolume/buyordervolume/hourstosold.

    El mapper los pone en 0 duro. Calcular el score ahí daría un número que diría
    "ilíquido" cuando la verdad es "no hay datos".
    """
    item = _map_topmovers_item({
        "markethashname": "AK-47 | Redline (Field-Tested)",
        "price": 1.50,
        "change24h": 0.03,
    })

    assert item["liquidityScore"] is None
    assert item["liquidityBreakdown"] is None


def test_movers_select_pide_los_campos_del_score():
    """Sin `prices` en el select, el mismo ítem puntúa distinto en Market que en Inventario.

    Los 4 componentes restantes se renormalizarían sobre 0.90 solo en el Market.
    Un score que cambia según la pantalla en la que lo mirás es un bug.
    """
    campos = set(_MOVERS_SELECT.split(","))

    assert {
        "sold24h", "sold7d", "offervolume", "buyordervolume",
        "buyorderprice", "hourstosold", "prices",
    } <= campos
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `python -m pytest tests/test_liquidity.py -v`
Expected: FAIL — `KeyError: 'liquidityScore'` en los dos primeros, y `AssertionError` en el del select (falta `prices`).

- [ ] **Step 3: Conectar `compute_liquidity` en `steam/mappers.py`**

Agregar el import junto a los demás, al principio del archivo:

```python
from steam.liquidity import compute_liquidity
```

En `_map_item`, reemplazar el `return {` por el cálculo previo:

```python
def _map_item(item: dict) -> dict:
    # /float/assets?with_items=1 nests market data under "item"; /inventory is flat
    d = item.get("item") or item

    latest = (
        d.get("pricelatestsell") or
        d.get("price") or
        d.get("lowestprice") or
        d.get("priceusd") or
        0
    )
    real = d.get("pricereal")
    float_data = item.get("float") or d.get("float") or {}

    # Sobre `d` crudo, no sobre el dict mapeado: abajo `sold24h` y compañía colapsan
    # None a 0, y eso borraría la diferencia entre "no hay datos" y "cero ventas".
    liquidity_score, liquidity_breakdown = compute_liquidity(d)

    return {
        ...
```

Y agregar los dos campos al dict que devuelve, justo debajo de `"hoursToSold"`:

```python
        "hoursToSold":    d.get("hourstosold") or 0,
        "liquidityScore":     liquidity_score,
        "liquidityBreakdown": liquidity_breakdown,
```

En `_map_topmovers_item`, agregar debajo de su `"hoursToSold": 0,`:

```python
        "hoursToSold":    0,
        # El payload de topmovers no trae offervolume/buyordervolume/hourstosold.
        # Un score calculado sobre ceros diría "ilíquido" en vez de "no hay datos".
        "liquidityScore":     None,
        "liquidityBreakdown": None,
```

- [ ] **Step 4: Agregar `prices` a `_MOVERS_SELECT` en `steam/routes/market.py`**

Reemplazar la línea 51:

```python
    "offervolume", "buyordervolume", "buyorderprice",
```

por:

```python
    "offervolume", "buyordervolume", "buyorderprice",
    # `prices` alimenta el componente de consistencia entre mercados del Liquidity
    # Score. Sin este campo, el Market renormaliza sobre 0.90 y el mismo ítem puntúa
    # distinto que en el Inventario.
    "prices",
```

- [ ] **Step 5: Correr toda la suite**

Run: `python -m pytest tests/ -v`
Expected: PASS (13 passed — 9 de liquidity + 4 de price_deltas). Los tests de `test_price_deltas.py` **no deben romperse**: si alguno falla, el cambio en `_map_item` rompió algo.

- [ ] **Step 6: Actualizar `CLAUDE.md`**

En la sección "Commands", reemplazar la línea `There are no test or lint commands configured.` por:

```markdown
```bash
# Tests
python -m pytest tests/ -v
```

There is no lint command configured.
```

En "Module structure", agregar debajo de la línea de `mappers.py`:

```
    liquidity.py    # Liquidity Score (0-100): compute_liquidity. Puro, sin deps internas.
```

En "Dependency order", agregar `steam/liquidity.py` a la primera línea (la de "nothing internal") y hacer que `steam/mappers.py` dependa de él:

```
settings.py, stores.py, middleware.py, steam/liquidity.py  ← nothing internal
steam/mappers.py        ← steam/liquidity
```

En "Data mapping", agregar al final:

```markdown
**Liquidity Score** (`steam/liquidity.py`): `liquidityScore` (0-100) responde "si listo este ítem hoy, ¿en cuánto se vende y a qué precio real?". Cinco componentes ponderados: velocidad de ventas (0.30), tiempo de venta (0.25), haircut contra el mejor bid (0.25), buy orders en espera (0.10), consistencia entre mercados (0.10). Cuando falta un componente sus pesos se **renormalizan** entre los disponibles; si queda menos del 50% del peso, el score es `None` → `"N/A"` (misma convención que los price deltas). `compute_liquidity` recibe el dict **crudo** de steamwebapi, no el mapeado: `_map_item` colapsa `None` a `0` y eso borraría la diferencia entre "no hay datos" y "cero ventas". `_map_topmovers_item` devuelve `None` porque su payload no trae los campos. **`_MOVERS_SELECT` debe incluir `prices`**, o el Market renormaliza sobre 0.90 y el mismo ítem puntúa distinto que en el Inventario. Ver `docs/superpowers/specs/2026-07-14-liquidity-score-design.md`.
```

- [ ] **Step 7: Correr la suite otra vez y commitear**

Run: `python -m pytest tests/ -v`
Expected: PASS (13 passed)

```bash
git add steam/liquidity.py steam/mappers.py steam/routes/market.py tests/test_liquidity.py CLAUDE.md
git commit -m "feat: exponer liquidityScore en /inventory y /market

_MOVERS_SELECT ahora pide `prices`: sin ese campo el componente de consistencia
entre mercados se descartaba solo en el Market y el mismo ítem puntuaba distinto
según la pantalla.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Contrato del frontend

**Files:**
- Modify: `CS-FINANCE-ionic/src/app/models/interfaces.ts:135`
- Modify: `CS-FINANCE-ionic/src/app/features/inventory/data/inventory.mock.ts` (6 ítems)
- Modify: `CS-FINANCE-ionic/src/app/features/market/data/market.mock.ts` (4 ítems)
- Modify: `CS-FINANCE-ionic/src/app/features/inventory/pages/inventory.spec.ts:24`
- Modify: `CS-FINANCE-ionic/src/app/features/market/pages/market.spec.ts:25`

**Interfaces:**
- Consumes: los campos `liquidityScore` y `liquidityBreakdown` que la Task 3 agrega al payload de la API.
- Produces: `ISkinCard.liquidityScore: number | null` y `ISkinCard.liquidityBreakdown`.

**La UI queda fuera de alcance.** Esta task solo tipa el contrato: `ISkinCard` tiene todos sus campos requeridos, así que agregar uno rompe el typecheck de los 10 literales de los mocks y los 2 fixtures de los specs. Sin esta task, el campo llega del backend pero TypeScript no lo conoce.

Todos los comandos de esta task se corren desde `CS-FINANCE-ionic/`.

- [ ] **Step 1: Agregar los campos a `interfaces.ts`**

Reemplazar la línea 135 (`hoursToSold: number;`) por:

```ts
  hoursToSold: number;          // <- `hourstosold` (liquidez: horas hasta que se vende)

  // ─── Liquidity Score (calculado en backend) ───────────────────
  liquidityScore: number | null;   // 0-100. null = datos insuficientes → "N/A"
  liquidityBreakdown: {            // null cuando liquidityScore es null
    [component: string]: { value: number; weight: number };
  } | null;
```

- [ ] **Step 2: Verificar que el typecheck rompe**

Run: `npm run build`
Expected: FAIL — errores `Property 'liquidityScore' is missing in type ...` en `inventory.mock.ts`, `market.mock.ts`, `inventory.spec.ts` y `market.spec.ts`.

- [ ] **Step 3: Actualizar los 10 mocks**

En `src/app/features/inventory/data/inventory.mock.ts`, debajo de cada una de las 6 líneas `hoursToSold: N,` (líneas 43, 88, 133, 178, 223, 268) agregar:

```ts
    liquidityScore: null,
    liquidityBreakdown: null,
```

En `src/app/features/market/data/market.mock.ts`, lo mismo debajo de cada una de las 4 líneas `hoursToSold: N,` (líneas 87, 132, 177, 222).

`null` es correcto para los mocks: son datos falsos, no tienen un score real. La UI los renderizará como `"N/A"`, que es exactamente lo que son.

- [ ] **Step 4: Actualizar los 2 fixtures de los specs**

En `src/app/features/inventory/pages/inventory.spec.ts:24` y `src/app/features/market/pages/market.spec.ts:25`, la línea:

```ts
    hoursToSold: 0, marketable: true, tradable: true, tradeLockDays: null, steamUrl: null,
```

pasa a:

```ts
    hoursToSold: 0, marketable: true, tradable: true, tradeLockDays: null, steamUrl: null,
    liquidityScore: null, liquidityBreakdown: null,
```

- [ ] **Step 5: Verificar build y tests**

Run: `npm run build`
Expected: PASS (sin errores de TypeScript)

Run: `npm test -- --watch=false --browsers=ChromeHeadless`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/app/models/interfaces.ts src/app/features/inventory src/app/features/market
git commit -m "feat: tipar liquidityScore en el contrato del frontend

El backend ya lo devuelve en /inventory y /market. La UI del badge es un
trabajo aparte.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Verificación final

Con las 4 tasks completas:

```bash
# Backend (desde LoginCsfinance/)
python -m pytest tests/ -v        # 13 passed
python run_dev.py                 # levantar y pegarle a /inventory con un token
```

El score real de tu inventario es la primera validación con datos de verdad. Lo que hay que mirar no es el número absoluto —no hay contra qué compararlo— sino **el ranking**: que las cases y las skins baratas de alta rotación queden arriba, y los knives caros y los souvenirs abajo. Si el orden tiene sentido, las constantes están bien calibradas. Si no, los cuatro números del bloque de anclas en `liquidity.py` son el lugar donde se ajusta.
