# Predicción de tendencia de precios como tool de Sharky — Design

**Fecha:** 2026-07-17
**Rama:** `feat/rag-chat` (commits locales, sin push/merge)
**Estado:** aprobado, listo para plan de implementación

## Resumen

Dar a Sharky (el chat de CS-FINANCE) una herramienta que estime la **tendencia de
precio a 7 días** (sube / baja / estable, con nivel de confianza) de una skin de CS2.
El usuario pregunta en lenguaje natural ("¿va a subir la AK Redline?") y Gemini,
mediante *function calling*, invoca la tool, que calcula la tendencia sobre el
histórico reciente y devuelve un veredicto que Sharky parafrasea.

Es la **semilla del Módulo 3** (orquestador con function calling): se implementa
con **una sola tool**, pero dejando el andamiaje para sumar más después.

## Objetivo y alcance

**Objetivo:** feature end-to-end funcional y honesta con los datos disponibles hoy
(~50 días de histórico diario por skin), sin esperar a acumular meses.

**En alcance:**
- Cálculo de tendencia (regresión lineal sobre el histórico + confianza).
- Servicio que baja el histórico y arma la respuesta.
- Integración con Gemini como una tool (function calling) en `/rag/chat`.
- Tests unitarios (TDD) de las tres piezas con red/DB mockeadas.

**Fuera de alcance (YAGNI):**
- Badge de tendencia en la UI o precómputo para todo el catálogo.
- Clasificador entrenado (sklearn) — futuro, cuando `precios_historicos` acumule
  meses de datos (>50 días, más allá de la ventana de `csfloat/history`).
- Cadenas de múltiples tools o tools adicionales (`consultar_precio_actual`,
  `buscar_contexto_rag`) — solo se deja el andamiaje listo.
- Predicción de giros/eventos (para eso está el RAG de noticias). Esto detecta
  **inercia**: proyecta la tendencia reciente, no anticipa cambios de rumbo.

## Contexto / datos

- Fuente v1: `market/csfloat/history` de steamwebapi (plan Starter de pago),
  vía la función existente `steam/services.py:_fetch_history_for_item`, que ya
  baja el histórico por skin con caché. Devuelve puntos diarios
  `{date, price, volume}`. **Límite conocido:** ventana móvil de ~50 días
  (aunque se pida más, devuelve ~50-52 puntos). Ver memoria
  `csfloat-history-50d-window`.
- No depende de la tabla `precios_historicos` ni de que el recolector
  (`price_capture`) haya corrido. Esa tabla será la fuente *futura* para
  tendencias de largo plazo y el enfoque entrenado.

## Arquitectura

Tres unidades con responsabilidad única:

### 1. `predict/trend.py` — cálculo puro (el "modelo")

Función sin red ni DB. Entrada: lista de puntos `{date, price}` (ya ordenados o
los ordena). Salida: veredicto estructurado. 100% testeable con datos sintéticos.

Pasos:
1. **Limpieza:** descartar puntos con `price <= 0` o absurdamente lejos de la
   mediana (defensa ante basura de datos, cf. `steamwebapi-pricelatestsell-flat`).
2. **Regresión lineal** por mínimos cuadrados: `precio ≈ a + b·día` (día = índice
   0..n-1). `b` = pendiente en €/día. **Python puro** (fórmula de mínimos cuadrados,
   ~10 líneas) — sin dependencias nuevas (numpy no está en `requirements.txt`).
3. **Proyección a 7 días en %:** `cambio_estimado_pct = (b * HORIZONTE_DIAS) / precio_actual * 100`.
   `precio_actual` = último precio de la serie.
4. **Clasificación** con umbral `UMBRAL_PCT` (default 2.0):
   - `cambio_pct > +UMBRAL_PCT` → `"sube"`
   - `cambio_pct < -UMBRAL_PCT` → `"baja"`
   - en medio → `"estable"`
5. **Confianza** vía R² del ajuste (0..1):
   - `R² >= R2_ALTA` (0.5) y clasificación no-estable → `"alta"`
   - `R² >= R2_MEDIA` (0.2) → `"media"`
   - si no → `"baja"`

**Salvaguardas:**
- `< MIN_PUNTOS` (14) puntos válidos → `tendencia: "desconocida"`, `motivo: "datos insuficientes"`.
- Nunca lanza por datos malos: si no puede calcular, devuelve `desconocida`.

**Constantes con nombre** al tope del archivo: `HORIZONTE_DIAS=7`, `UMBRAL_PCT=2.0`,
`R2_ALTA=0.5`, `R2_MEDIA=0.2`, `MIN_PUNTOS=14`. Fáciles de ajustar.

**Retorno:**
```python
{
  "tendencia": "sube" | "baja" | "estable" | "desconocida",
  "confianza": "alta" | "media" | "baja",
  "cambio_estimado_pct": float,   # redondeado
  "precio_actual": float,
  "dias_analizados": int,
  "motivo": str | None,           # solo si "desconocida"
}
```

### 2. `predict/service.py` — orquestador de la tool

`async def predecir_tendencia(client, name) -> dict`:
1. Baja el histórico reutilizando `_fetch_history_for_item(client, name)`. Ese
   fetch hoy pide 35 días; el plan lo **parametriza para pedir ~50 días** (la
   ventana completa que da `csfloat/history`) sin romper su uso actual en los
   price-deltas. 35 días ya bastan (>14 pts), pero 50 aprovechan todo el dato.
2. Si vacío → `{tendencia: "desconocida", motivo: "sin histórico para esa skin"}`.
3. Si no, lo pasa a `trend.calcular(...)` y devuelve el veredicto.
Best-effort: cualquier excepción (429, red, formato) → `desconocida` con motivo,
sin propagar.

### 3. Integración con Gemini (function calling) en `rag/gemini.py` + `rag/router.py`

Extender `generate_reply` (o añadir variante) para soportar tools en flujo de dos pasos:
1. Primera llamada a `generateContent` con `tools: [{functionDeclarations: [TREND_TOOL]}]`.
   - `TREND_TOOL`: nombre `predecir_tendencia_skin`, descripción que instruye a
     Gemini a construir el `market_hash_name` canónico (ej. `"AK-47 | Redline (Field-Tested)"`),
     parámetro `market_hash_name: string`.
2. La respuesta trae texto o un `functionCall`:
   - **Texto** → devolver tal cual.
   - **functionCall** → ejecutar `service.predecir_tendencia`, mandar el resultado
     como `functionResponse` en una segunda llamada, y devolver el texto final.
3. **Un solo turno de tool** por mensaje (sin cadenas).
4. **Registro de tools** (dict nombre→callable), no un `if` hardcodeado, para
   extender a futuras tools sin rehacer el andamiaje.

Vive en el endpoint conversacional existente **`/rag/chat`** → el frontend Sharky
no cambia.

## Flujo de datos

```
Usuario: "¿va a subir la AK Redline?"
  → /rag/chat → generate_reply (con TREND_TOOL declarada)
     → Gemini pide functionCall predecir_tendencia_skin("AK-47 | Redline (Field-Tested)")
       → service.predecir_tendencia → _fetch_history_for_item (~50d, cacheado)
                                     → trend.calcular → {sube, media, +3.2%, ...}
       → functionResponse de vuelta a Gemini (2ª llamada)
     → Gemini redacta: "La AK Redline viene ligeramente al alza (~+3% a 7 días),
        con confianza media porque el precio se movió irregular."
```

## Manejo de errores (best-effort, nunca romper el chat)

| Situación | Resultado |
|---|---|
| Skin no resuelve / histórico vacío | `desconocida` → Sharky pide el nombre exacto |
| `csfloat/history` falla o 429 | capturado → `desconocida` con motivo; chat sigue |
| Gemini no emite function-call bien | fallback: responde como chat normal sin tool |
| `< 14` puntos | veredicto honesto "no tengo suficiente historial" |

## Testing (TDD)

- **`trend.py`**: unit tests sintéticos — serie al alza→sube, a la baja→baja,
  plana→estable/baja confianza, `<14` pts→desconocida, outliers descartados,
  R² alto/bajo→confianza alta/baja. Sin red ni DB.
- **`service.py`**: `_fetch_history_for_item` mockeado (AsyncMock) — respuesta bien
  armada y camino "histórico vacío→desconocida".
- **`gemini.py`**: HTTP de Gemini mockeada — que un `functionCall` dispare la tool
  y la 2ª llamada; que una respuesta de texto pase directo.
- Suite completa verde (`venv\Scripts\python -m pytest`) antes de cada commit.

## Constraints operativos

- Commits locales en `feat/rag-chat` con mensaje `feat(predict): ...` terminado en
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. **Sin push/merge.**
- Stack liviano/gratis: sin dependencias pesadas nuevas (numpy solo si ya está;
  si no, mínimos cuadrados a mano). Respetar el `_history_limiter` compartido.
- Archivos nuevos en UTF-8.

## Camino de upgrade (futuro, no ahora)

Cuando `precios_historicos` acumule meses: (a) la tool puede leer de esa tabla para
tendencias de largo plazo; (b) se habilita el enfoque B (clasificador entrenado);
(c) se suman más tools al registro (`consultar_precio_actual`, `buscar_contexto_rag`)
completando el Módulo 3.
