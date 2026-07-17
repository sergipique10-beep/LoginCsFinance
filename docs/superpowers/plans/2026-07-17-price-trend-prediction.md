# Predicción de tendencia como tool de Sharky — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dar a Sharky una tool (`predecir_tendencia_skin`) que estime la tendencia de precio a 7 días (sube/baja/estable + confianza) de una skin de CS2, invocada por Gemini vía function calling en `/rag/chat`.

**Architecture:** Tres unidades: `predict/trend.py` (cálculo puro por regresión lineal, sin red/DB), `predict/service.py` (baja el histórico reutilizando `_fetch_history_for_item` y arma el veredicto), y function calling en `rag/gemini.py` (declara la tool a Gemini, ejecuta un turno de tool, redacta la respuesta). Fuente de datos: `csfloat/history` en vivo (~50d), sin depender de `precios_historicos`.

**Tech Stack:** Python 3 / FastAPI, httpx.AsyncClient compartido, Gemini `generateContent` (function calling), pytest + pytest-asyncio. Sin dependencias nuevas (mínimos cuadrados en Python puro; numpy NO está en requirements).

## Global Constraints

- **Commits locales SÍ; push y merge NO.** Cada tarea termina en commit local en `feat/rag-chat`. Mensaje `feat(predict): ...`, cuerpo terminado en `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Nunca `git push`/`git merge`.
- Tests: `venv\Scripts\python -m pytest` (el Python del sistema no tiene firebase_admin). `pytest-asyncio` con `asyncio_mode=auto` ya configurado. Correr la suite completa antes de cada commit.
- Sin dependencias nuevas. Python puro para la matemática.
- Archivos nuevos en UTF-8. No tocar la codificación de `requirements.txt`.
- Best-effort: ningún fallo de la tool (red, 429, datos malos) puede tumbar el chat.
- Constantes de tuning con nombre al tope de `trend.py`: `HORIZONTE_DIAS=7`, `UMBRAL_PCT=2.0`, `R2_ALTA=0.5`, `R2_MEDIA=0.2`, `MIN_PUNTOS=14`, `MEDIAN_FACTOR=5.0`.

## File Structure

- `predict/__init__.py` — **crear** (paquete vacío).
- `predict/trend.py` — **crear**. Cálculo puro `calcular(points) -> dict`.
- `predict/service.py` — **crear**. `predecir_tendencia(client, name) -> dict`.
- `steam/services.py` — **modificar**. Parametrizar `_fetch_history_for_item` con `days` (default 35, cache key usa el valor).
- `rag/gemini.py` — **modificar**. `TREND_TOOL`, registro `_TOOLS`, function calling en `generate_reply`.
- `tests/test_trend.py`, `tests/test_predict_service.py`, `tests/test_chat_tool.py` — **crear**.

---

## Task 1: Cálculo de tendencia (`predict/trend.py`)

**Files:**
- Create: `predict/__init__.py` (vacío)
- Create: `predict/trend.py`
- Test: `tests/test_trend.py`

**Interfaces:**
- Produces: `def calcular(points: list[dict]) -> dict`. `points` = `[{"date": str, "price": float}, ...]` (acepta claves extra como `volume`). Devuelve
  `{"tendencia": "sube"|"baja"|"estable"|"desconocida", "confianza": "alta"|"media"|"baja", "cambio_estimado_pct": float, "precio_actual": float, "dias_analizados": int, "motivo": str|None}`.

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/test_trend.py`:

```python
from predict import trend


def _serie(prices):
    return [{"date": f"2026-05-{i+1:02d}", "price": p} for i, p in enumerate(prices)]


def test_serie_al_alza_es_sube():
    out = trend.calcular(_serie([float(10 + i) for i in range(20)]))  # +1/día claro
    assert out["tendencia"] == "sube"
    assert out["confianza"] == "alta"          # recta casi perfecta → R² alto
    assert out["cambio_estimado_pct"] > 0
    assert out["dias_analizados"] == 20


def test_serie_a_la_baja_es_baja():
    out = trend.calcular(_serie([float(40 - i) for i in range(20)]))
    assert out["tendencia"] == "baja"
    assert out["cambio_estimado_pct"] < 0


def test_serie_plana_es_estable():
    out = trend.calcular(_serie([20.0] * 20))
    assert out["tendencia"] == "estable"


def test_pocos_puntos_es_desconocida():
    out = trend.calcular(_serie([10.0, 11.0, 12.0]))   # 3 < MIN_PUNTOS
    assert out["tendencia"] == "desconocida"
    assert out["motivo"]


def test_descarta_outliers_y_ceros():
    prices = [float(10 + i) for i in range(20)]
    prices[5] = 0.0            # cero → se descarta
    prices[10] = 99999.0       # disparado (>5x mediana) → se descarta
    out = trend.calcular(_serie(prices))
    assert out["tendencia"] == "sube"          # sigue viéndose la tendencia
    assert out["dias_analizados"] == 18        # 20 - 2 descartados


def test_serie_ruidosa_baja_confianza():
    # sube en promedio pero muy irregular → R² bajo → confianza baja
    prices = [10, 18, 11, 19, 12, 20, 13, 21, 14, 22, 15, 23, 16, 24, 17, 25]
    out = trend.calcular(_serie([float(p) for p in prices]))
    assert out["confianza"] in ("baja", "media")


def test_lista_vacia_es_desconocida():
    out = trend.calcular([])
    assert out["tendencia"] == "desconocida"
```

- [ ] **Step 2: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_trend.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'predict'`.

- [ ] **Step 3: Crear el paquete**

Crear `predict/__init__.py` vacío.

- [ ] **Step 4: Implementar `predict/trend.py`**

```python
"""Cálculo de tendencia de precio por regresión lineal (Python puro, sin numpy).

Detecta INERCIA: proyecta la pendiente reciente a HORIZONTE_DIAS. No anticipa
giros ni noticias. Honesto con pocos datos: por debajo de MIN_PUNTOS no opina.
"""
from statistics import median

HORIZONTE_DIAS = 7      # a cuántos días se proyecta la tendencia
UMBRAL_PCT = 2.0        # |cambio proyectado| < esto → "estable"
R2_ALTA = 0.5           # R² para confianza "alta" (además de no-estable)
R2_MEDIA = 0.2          # R² para confianza "media"
MIN_PUNTOS = 14         # menos de esto → "desconocida"
MEDIAN_FACTOR = 5.0     # descarta precios > mediana*factor o < mediana/factor


def _limpiar(points: list[dict]) -> list[float]:
    """Precios válidos en orden: descarta <=0 y outliers absurdos vs mediana."""
    precios = [float(p["price"]) for p in points if p.get("price") not in (None, "")]
    precios = [p for p in precios if p > 0]
    if not precios:
        return []
    med = median(precios)
    if med <= 0:
        return precios
    lo, hi = med / MEDIAN_FACTOR, med * MEDIAN_FACTOR
    return [p for p in precios if lo <= p <= hi]


def _desconocida(motivo: str, n: int = 0) -> dict:
    return {"tendencia": "desconocida", "confianza": "baja",
            "cambio_estimado_pct": 0.0, "precio_actual": 0.0,
            "dias_analizados": n, "motivo": motivo}


def calcular(points: list[dict]) -> dict:
    precios = _limpiar(points)
    n = len(precios)
    if n < MIN_PUNTOS:
        return _desconocida("datos insuficientes", n)

    precio_actual = precios[-1]
    if precio_actual <= 0:
        return _desconocida("precio actual inválido", n)

    # Mínimos cuadrados: precio ≈ a + b·x, con x = 0..n-1
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(precios) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, precios))
    b = sxy / sxx if sxx else 0.0          # pendiente €/día
    a = my - b * mx

    # R²: qué tan bien la recta explica los datos
    ss_tot = sum((y - my) ** 2 for y in precios)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, precios))
    r2 = 1.0 if ss_tot == 0 else max(0.0, 1 - ss_res / ss_tot)

    cambio_pct = (b * HORIZONTE_DIAS) / precio_actual * 100

    if cambio_pct > UMBRAL_PCT:
        tendencia = "sube"
    elif cambio_pct < -UMBRAL_PCT:
        tendencia = "baja"
    else:
        tendencia = "estable"

    if tendencia != "estable" and r2 >= R2_ALTA:
        confianza = "alta"
    elif r2 >= R2_MEDIA:
        confianza = "media"
    else:
        confianza = "baja"

    return {"tendencia": tendencia, "confianza": confianza,
            "cambio_estimado_pct": round(cambio_pct, 2),
            "precio_actual": round(precio_actual, 2),
            "dias_analizados": n, "motivo": None}
```

- [ ] **Step 5: Correr el test para verlo pasar**

Run: `venv\Scripts\python -m pytest tests/test_trend.py -v`
Expected: PASS (7 passed).

- [ ] **Step 6: Commit local (sin push)**

```bash
git add predict/__init__.py predict/trend.py tests/test_trend.py
git commit -m "feat(predict): cálculo de tendencia por regresión lineal (puro)"
```

---

## Task 2: Fetch parametrizado + servicio (`predict/service.py`)

**Files:**
- Modify: `steam/services.py` (`_fetch_history_for_item`: añadir parámetro `days`)
- Create: `predict/service.py`
- Test: `tests/test_predict_service.py`

**Interfaces:**
- Consumes: `steam.services._fetch_history_for_item(client, name, days=35) -> list[dict]` (cada dict `{"date","price","volume"}`); `predict.trend.calcular`.
- Produces: `async def predecir_tendencia(client, name: str) -> dict` — mismo shape que `trend.calcular`, con `"desconocida"` best-effort ante histórico vacío o excepción.

- [ ] **Step 1: Parametrizar `_fetch_history_for_item` en `steam/services.py`**

En `steam/services.py`, la función arranca (L74-75) con `days` hardcodeado a 35. Cambiar la firma y usar `days` tanto en el cache key como en `start_date`:

```python
async def _fetch_history_for_item(client: httpx.AsyncClient, name: str, days: int = 35) -> list:
    cache_key = f"{name}:csfloat:{days}d"
```

y en la construcción de params (L91):

```python
                "start_date": (today - timedelta(days=days)).isoformat(),
```

(El resto de la función queda igual. Los llamadores actuales sin `days` siguen usando 35 → sin cambios de comportamiento.)

- [ ] **Step 2: Verificar que nada se rompió**

Run: `venv\Scripts\python -m pytest tests/ -q`
Expected: toda la suite sigue verde (los tests existentes de history usan el default 35).

- [ ] **Step 3: Escribir el test que falla**

Crear `tests/test_predict_service.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from predict import service


@pytest.mark.asyncio
async def test_predecir_tendencia_ok(monkeypatch):
    puntos = [{"date": f"2026-05-{i+1:02d}", "price": float(10 + i), "volume": 5}
              for i in range(20)]
    monkeypatch.setattr(service, "_fetch_history_for_item", AsyncMock(return_value=puntos))
    out = await service.predecir_tendencia(MagicMock(), "AK-47 | Redline (Field-Tested)")
    assert out["tendencia"] == "sube"
    assert out["dias_analizados"] == 20


@pytest.mark.asyncio
async def test_predecir_tendencia_historico_vacio(monkeypatch):
    monkeypatch.setattr(service, "_fetch_history_for_item", AsyncMock(return_value=[]))
    out = await service.predecir_tendencia(MagicMock(), "Skin Fantasma")
    assert out["tendencia"] == "desconocida"
    assert out["motivo"]


@pytest.mark.asyncio
async def test_predecir_tendencia_captura_excepcion(monkeypatch):
    monkeypatch.setattr(service, "_fetch_history_for_item",
                        AsyncMock(side_effect=RuntimeError("boom")))
    out = await service.predecir_tendencia(MagicMock(), "AK-47 | Redline (Field-Tested)")
    assert out["tendencia"] == "desconocida"
    assert out["motivo"]


@pytest.mark.asyncio
async def test_predecir_tendencia_pide_50_dias(monkeypatch):
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(service, "_fetch_history_for_item", fetch)
    await service.predecir_tendencia(MagicMock(), "AK-47 | Redline (Field-Tested)")
    assert fetch.await_args.kwargs.get("days") == 50 or fetch.await_args.args[2:] == (50,)
```

- [ ] **Step 4: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_predict_service.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'predict.service'`.

- [ ] **Step 5: Implementar `predict/service.py`**

```python
"""Servicio de la tool de predicción: baja el histórico y calcula la tendencia.

Best-effort: cualquier fallo (histórico vacío, 429, red, formato) devuelve un
veredicto "desconocida" con motivo, nunca propaga — el chat no debe caerse.
"""
import logging

from steam.services import _fetch_history_for_item
from predict.trend import calcular

logger = logging.getLogger("uvicorn.error")

_DIAS_HISTORICO = 50   # ventana completa que da csfloat/history


async def predecir_tendencia(client, name: str) -> dict:
    """Estima la tendencia de precio a 7 días de la skin `name` (market_hash_name)."""
    try:
        puntos = await _fetch_history_for_item(client, name, days=_DIAS_HISTORICO)
    except Exception as exc:  # noqa: BLE001 — best-effort, nunca romper el chat
        logger.warning("[predict] histórico falló para %r: %s", name, exc)
        return {"tendencia": "desconocida", "confianza": "baja",
                "cambio_estimado_pct": 0.0, "precio_actual": 0.0,
                "dias_analizados": 0, "motivo": "no se pudo obtener el histórico"}

    if not puntos:
        return {"tendencia": "desconocida", "confianza": "baja",
                "cambio_estimado_pct": 0.0, "precio_actual": 0.0,
                "dias_analizados": 0, "motivo": "sin histórico para esa skin"}

    return calcular(puntos)
```

- [ ] **Step 6: Correr el test para verlo pasar**

Run: `venv\Scripts\python -m pytest tests/test_predict_service.py -v`
Expected: PASS (4 passed).

- [ ] **Step 7: Commit local (sin push)**

```bash
git add steam/services.py predict/service.py tests/test_predict_service.py
git commit -m "feat(predict): servicio de tendencia + ventana de histórico parametrizable"
```

---

## Task 3: Function calling en el chat (`rag/gemini.py`)

**Files:**
- Modify: `rag/gemini.py` (`generate_reply`: declarar la tool y ejecutar un turno de tool)
- Test: `tests/test_chat_tool.py`

**Interfaces:**
- Consumes: `predict.service.predecir_tendencia(client, name)`.
- Produces: `generate_reply(client, message, history) -> str` (misma firma) ahora resuelve un `functionCall` de Gemini ejecutando la tool. Registro module-level `_TOOLS: dict[str, callable]` y declaración `_TOOL_DECLS`.
- El endpoint `/rag/chat` en `rag/router.py` **no cambia** (ya llama a `generate_reply`).

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/test_chat_tool.py`. Simula la HTTP de Gemini: primera respuesta pide `functionCall`, segunda devuelve texto. Usa el `client` fixture de `tests/conftest.py` solo para el camino router; para la lógica unitaria mockeamos `httpx.AsyncClient`.

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from rag import gemini


def _resp(payload):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=payload)
    return r


def _text_answer(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _function_call(name, args):
    return {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": name, "args": args}}]}}]}


@pytest.mark.asyncio
async def test_texto_normal_pasa_directo(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "k")
    client = MagicMock()
    client.post = AsyncMock(return_value=_resp(_text_answer("¡Hola! Soy Sharky.")))
    out = await gemini.generate_reply(client, "hola", [])
    assert out == "¡Hola! Soy Sharky."
    assert client.post.await_count == 1          # sin tool → una sola llamada


@pytest.mark.asyncio
async def test_functioncall_ejecuta_tool_y_redacta(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "k")
    # la tool devuelve un veredicto fijo
    fake_tool = AsyncMock(return_value={"tendencia": "sube", "confianza": "media",
                                        "cambio_estimado_pct": 3.2, "precio_actual": 28.5,
                                        "dias_analizados": 50, "motivo": None})
    monkeypatch.setitem(gemini._TOOLS, "predecir_tendencia_skin", fake_tool)

    client = MagicMock()
    client.post = AsyncMock(side_effect=[
        _resp(_function_call("predecir_tendencia_skin",
                             {"market_hash_name": "AK-47 | Redline (Field-Tested)"})),
        _resp(_text_answer("La AK Redline viene al alza (~+3% a 7 días).")),
    ])

    out = await gemini.generate_reply(client, "¿sube la AK Redline?", [])

    assert "alza" in out
    assert client.post.await_count == 2          # tool-call + redacción
    fake_tool.assert_awaited_once()
    assert fake_tool.await_args.args[1] == "AK-47 | Redline (Field-Tested)"
```

- [ ] **Step 2: Correr el test para verlo fallar**

Run: `venv\Scripts\python -m pytest tests/test_chat_tool.py -v`
Expected: FAIL (`gemini._TOOLS` no existe / no hay manejo de functionCall).

- [ ] **Step 3: Implementar function calling en `rag/gemini.py`**

Añadir cerca del tope del módulo (tras `_SYSTEM_PROMPT`), la importación diferida del servicio para evitar ciclos y declarar tool + registro:

```python
from predict.service import predecir_tendencia as _tool_tendencia

# Declaración de tools para Gemini (function calling). Semilla del Módulo 3:
# para sumar tools, agregar aquí y en _TOOLS — sin tocar el flujo.
_TOOL_DECLS = [{
    "name": "predecir_tendencia_skin",
    "description": (
        "Estima la tendencia de precio (sube/baja/estable) a 7 días de una skin "
        "de CS2. Úsala cuando el usuario pregunte si una skin va a subir, bajar, "
        "si es buen momento, o por su evolución de precio. El parámetro "
        "market_hash_name debe ser el nombre EXACTO de mercado, p.ej. "
        "'AK-47 | Redline (Field-Tested)'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "market_hash_name": {"type": "string",
                                 "description": "Nombre de mercado exacto de la skin"},
        },
        "required": ["market_hash_name"],
    },
}]

# name -> callable(client, market_hash_name) -> dict
_TOOLS = {"predecir_tendencia_skin": _tool_tendencia}


def _extract_parts(data: dict) -> list[dict]:
    cands = data.get("candidates", [])
    if not cands:
        feedback = data.get("promptFeedback", {})
        logger.warning("Gemini sin candidates. promptFeedback=%s", feedback)
        raise RuntimeError("Gemini no devolvió respuesta (posible bloqueo de seguridad)")
    return cands[0].get("content", {}).get("parts", [])
```

Reescribir `generate_reply` para (a) mandar `tools`, (b) si vuelve un `functionCall`, ejecutar la tool y hacer la segunda llamada con el `functionResponse`:

```python
async def generate_reply(
    client: httpx.AsyncClient,
    message: str,
    history: list[dict],
) -> str:
    """Envía la conversación a Gemini (con tools) y devuelve el texto de respuesta.

    Si Gemini pide una function call, ejecuta la tool y hace una segunda llamada
    para que redacte la respuesta final. Un solo turno de tool por mensaje.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY no configurada")

    contents: list[dict] = []
    for turn in history:
        text = (turn.get("content") or "").strip()
        if not text:
            continue
        role = "model" if turn.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    body = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": contents,
        "tools": [{"functionDeclarations": _TOOL_DECLS}],
    }
    url = f"{_GEMINI_BASE}/{GEMINI_MODEL}:generateContent"

    async def _call(payload: dict) -> dict:
        resp = await client.post(
            url, headers={"x-goog-api-key": GEMINI_API_KEY},
            json=payload, timeout=_GEMINI_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    parts = _extract_parts(await _call(body))

    fcall = next((p["functionCall"] for p in parts if p.get("functionCall")), None)
    if fcall:
        name = fcall.get("name", "")
        args = fcall.get("args", {}) or {}
        tool = _TOOLS.get(name)
        if tool is not None:
            try:
                result = await tool(client, args.get("market_hash_name", ""))
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning("[chat] tool %s falló: %s", name, exc)
                result = {"tendencia": "desconocida",
                          "motivo": "la herramienta no está disponible ahora"}
            # 2ª llamada: devolvemos el resultado de la tool para que redacte
            contents.append({"role": "model", "parts": [{"functionCall": fcall}]})
            contents.append({"role": "user", "parts": [
                {"functionResponse": {"name": name, "response": result}}]})
            parts = _extract_parts(await _call({**body, "contents": contents}))

    text = "".join(p.get("text", "") for p in parts if p.get("text")).strip()
    if not text:
        raise RuntimeError("Respuesta vacía de Gemini")
    return text
```

(Borrar el cuerpo viejo de `generate_reply` que hacía la extracción inline de `candidates`/`parts` — ahora vive en `_extract_parts`.)

- [ ] **Step 4: Correr el test para verlo pasar**

Run: `venv\Scripts\python -m pytest tests/test_chat_tool.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Correr la suite completa**

Run: `venv\Scripts\python -m pytest tests/ -q`
Expected: toda la suite verde. Ojo especial a los tests previos de `/rag/chat` (si existen en `tests/`): la firma de `generate_reply` no cambió, así que deben seguir pasando; si alguno mockeaba `client.post` con una sola respuesta de texto, sigue valiendo (el `functionCall` es opcional).

- [ ] **Step 6: Commit local (sin push)**

```bash
git add rag/gemini.py tests/test_chat_tool.py
git commit -m "feat(predict): tool predecir_tendencia_skin en Sharky (function calling)"
```

---

## Cierre (manual, tras aprobar el operador)

No se ejecuta en esta sesión (sin push/merge). Para probar Sharky + RAG end-to-end hace falta el entorno vivo:

1. `GEMINI_API_KEY` y `STEAM_API_KEY` en `.env`/Render (ya deberían estar).
2. Levantar el backend y preguntarle a Sharky por una skin ("¿va a subir la AK Redline?") para ver el function calling en acción.
3. Verificar que el modelo `GEMINI_MODEL` configurado soporta function calling (los `gemini-*-flash` recientes sí; si diera error de tools, actualizar el nombre del modelo en `.env`).

---

## Self-Review (cobertura del spec)

- Cálculo puro por regresión lineal + confianza R² + salvaguardas (min puntos, outliers) → Task 1 ✅
- Constantes de tuning con nombre → Task 1 ✅
- Servicio best-effort reutilizando `_fetch_history_for_item`, ventana ~50d → Task 2 ✅
- Fuente en vivo `csfloat/history`, sin depender de `precios_historicos` → Task 2 ✅
- Function calling con un turno de tool + registro extensible (semilla Módulo 3) → Task 3 ✅
- `/rag/chat` sin cambios, frontend intacto → Task 3 (firma preservada) ✅
- Manejo de errores best-effort en las 3 capas (trend→desconocida, service→try/except, gemini→try/except) → Tasks 1-3 ✅
- TDD con red/DB mockeadas en cada pieza → Tasks 1-3 ✅
- Fuera de alcance (UI, clasificador entrenado, cadenas de tools) → no se toca ✅
