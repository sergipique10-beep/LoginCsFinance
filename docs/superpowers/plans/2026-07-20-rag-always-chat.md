# Sharky RAG-always: conectar el retrieval al chat — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que `POST /rag/chat` recupere contexto de las noticias (Supabase pgvector) en cada mensaje y se lo inyecte a Gemini, manteniendo el historial — sin function calling (enfoque RAG-always).

**Architecture:** Dos cambios: (1) `generate_reply` gana un parámetro opcional `context_chunks` que inyecta el contexto en el turno actual del usuario; (2) el router `rag_chat` llama `retrieve()` best-effort (un fallo del RAG no tumba el chat) y pasa los chunks. Reutiliza `retrieve` y `_format_context` existentes.

**Tech Stack:** Python 3 / FastAPI, httpx, Gemini REST, pgvector (vía `retrieve`), pytest + pytest-asyncio + TestClient.

## Global Constraints

- **Commits locales SÍ; push y merge NO.** Cada tarea termina en commit local en `feat/rag-chat`. Mensaje `feat(rag): ...`, cuerpo terminado en `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Nunca `git push`/`git merge`.
- Tests: `venv\Scripts\python -m pytest`. `pytest-asyncio` con `asyncio_mode=auto` ya configurado. Correr la suite completa antes de cada commit.
- Sin dependencias nuevas. Reutilizar `retrieve` (`rag/retrieval.py`) y `_format_context` (`rag/gemini.py`).
- Archivos UTF-8. Contrato `ChatResponse` = `{reply}` sin cambios (sin fuentes).
- Best-effort: un fallo de `retrieve` no puede romper el chat.

## File Structure

- `rag/gemini.py` — **modificar**. `_SYSTEM_PROMPT` (frase híbrida), `generate_reply` (param `context_chunks`), helper `_build_user_text`.
- `rag/router.py` — **modificar**. `rag_chat`: `retrieve` best-effort → pasa `context_chunks`.
- `tests/test_rag_gemini.py` — **modificar** (añadir tests de `generate_reply`).
- `tests/test_rag_router.py` — **modificar** (añadir tests de `/rag/chat`).

---

## Task 1: Inyección de contexto en `generate_reply`

**Files:**
- Modify: `rag/gemini.py` (`_SYSTEM_PROMPT`, nuevo `_build_user_text`, `generate_reply`)
- Test: `tests/test_rag_gemini.py`

**Interfaces:**
- Produces: `generate_reply(client, message, history, context_chunks=None) -> str`. Cuando `context_chunks` tiene elementos, el último turno de `contents` contiene un bloque `CONTEXTO` + `MENSAJE DEL USUARIO`; cuando es `None`/`[]`, el último turno es solo `message` (comportamiento actual).
- Consumes: `_format_context(chunks)` ya existente en `rag/gemini.py`.

- [ ] **Step 1: Escribir los tests que fallan**

Añadir a `tests/test_rag_gemini.py` (ya importa `AsyncMock`; añadir imports que falten):

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from rag import gemini


def _resp(text):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={"candidates": [{"content": {"parts": [{"text": text}]}}]})
    return r


@pytest.mark.asyncio
async def test_generate_reply_injects_context(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "k")
    client = MagicMock()
    client.post = AsyncMock(return_value=_resp("Cache volvió al Active Duty."))

    chunks = [{"title": "CS2 Update", "url": "https://u",
               "content": "Added Cache to the Active Duty Map Pool. Removed Overpass."}]
    out = await gemini.generate_reply(client, "¿por qué volvió Cache?", [], context_chunks=chunks)

    assert out == "Cache volvió al Active Duty."
    body = client.post.call_args.kwargs["json"]
    last_text = body["contents"][-1]["parts"][0]["text"]
    assert "CONTEXTO" in last_text
    assert "MENSAJE DEL USUARIO" in last_text
    assert "Added Cache" in last_text            # el contenido del chunk viaja
    assert "¿por qué volvió Cache?" in last_text  # el mensaje también


@pytest.mark.asyncio
async def test_generate_reply_without_context_is_plain_message(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "k")
    client = MagicMock()
    client.post = AsyncMock(return_value=_resp("¡Hola!"))

    out = await gemini.generate_reply(client, "hola", [])

    assert out == "¡Hola!"
    body = client.post.call_args.kwargs["json"]
    last_text = body["contents"][-1]["parts"][0]["text"]
    assert last_text == "hola"                    # sin bloque CONTEXTO
    assert "CONTEXTO" not in last_text


@pytest.mark.asyncio
async def test_generate_reply_empty_chunks_is_plain_message(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "k")
    client = MagicMock()
    client.post = AsyncMock(return_value=_resp("ok"))

    await gemini.generate_reply(client, "hola", [], context_chunks=[])

    last_text = client.post.call_args.kwargs["json"]["contents"][-1]["parts"][0]["text"]
    assert last_text == "hola"
```

- [ ] **Step 2: Correr los tests para verlos fallar**

Run: `venv\Scripts\python -m pytest tests/test_rag_gemini.py -v`
Expected: los 3 nuevos FALLAN (`generate_reply` no acepta `context_chunks` → `TypeError`).

- [ ] **Step 3: Ajustar `_SYSTEM_PROMPT` en `rag/gemini.py`**

Reemplazar el `_SYSTEM_PROMPT` actual (líneas 25-30) por:

```python
_SYSTEM_PROMPT = (
    "Eres Sharky 🦈, el asistente de CS-FINANCE, experto en el mercado de "
    "skins de Counter-Strike 2 (precios, tendencias, liquidez, noticias). "
    "Respondes en español, de forma clara y concisa. "
    "Cuando el CONTEXTO contenga información relevante, básate en ella para "
    "responder; si no aplica, responde con tu conocimiento general. "
    "Si no tienes datos suficientes para responder con certeza, dilo en lugar "
    "de inventar."
)
```

- [ ] **Step 4: Añadir `_build_user_text` y el parámetro en `generate_reply`**

En `rag/gemini.py`, añadir el helper justo antes de `generate_reply` (usa `_format_context`, que se resuelve en tiempo de ejecución aunque esté definido más abajo en el módulo):

```python
def _build_user_text(message: str, context_chunks: list[dict] | None) -> str:
    """Turno del usuario: con contexto RAG inyectado si lo hay, si no el mensaje pelado."""
    if context_chunks:
        return (
            "CONTEXTO (noticias recientes; úsalo si es relevante, ignóralo si no aplica):\n"
            f"{_format_context(context_chunks)}\n\n"
            f"MENSAJE DEL USUARIO:\n{message}"
        )
    return message
```

Cambiar la firma de `generate_reply` (línea 33) a:

```python
async def generate_reply(
    client: httpx.AsyncClient,
    message: str,
    history: list[dict],
    context_chunks: list[dict] | None = None,
) -> str:
```

y reemplazar la línea que arma el turno actual (hoy: `contents.append({"role": "user", "parts": [{"text": message}]})`) por:

```python
    contents.append({"role": "user", "parts": [{"text": _build_user_text(message, context_chunks)}]})
```

(El resto de `generate_reply` — construcción del historial, body, llamada, extracción de texto — queda igual.)

- [ ] **Step 5: Correr los tests para verlos pasar**

Run: `venv\Scripts\python -m pytest tests/test_rag_gemini.py -v`
Expected: PASS (los 3 nuevos + los existentes de `generate_with_context`).

- [ ] **Step 6: Commit local (sin push)**

```bash
git add rag/gemini.py tests/test_rag_gemini.py
git commit -m "feat(rag): generate_reply inyecta contexto RAG opcional + prompt híbrido"
```

---

## Task 2: Retrieval best-effort en `/rag/chat`

**Files:**
- Modify: `rag/router.py` (`rag_chat`)
- Test: `tests/test_rag_router.py`

**Interfaces:**
- Consumes: `retrieve(client, message)` (ya importado en `rag/router.py`), `generate_reply(client, message, history, context_chunks=...)` de Task 1.
- Produces: `POST /rag/chat` recupera contexto y lo pasa a `generate_reply`; si `retrieve` falla, responde igual sin contexto. Contrato `{reply}` intacto.

- [ ] **Step 1: Escribir los tests que fallan**

Añadir a `tests/test_rag_router.py` (ya importa `AsyncMock` y `rag_router`):

```python
def test_chat_passes_retrieved_context(client, monkeypatch):
    chunks = [{"title": "CS2 Update", "url": "https://u", "content": "Cache vuelve."}]
    monkeypatch.setattr(rag_router, "retrieve", AsyncMock(return_value=chunks))
    gen = AsyncMock(return_value="Cache volvió al Active Duty.")
    monkeypatch.setattr(rag_router, "generate_reply", gen)

    resp = client.post("/rag/chat", json={"message": "¿por qué volvió Cache?", "history": []})

    assert resp.status_code == 200
    assert resp.json()["reply"] == "Cache volvió al Active Duty."
    assert gen.await_args.kwargs["context_chunks"] == chunks


def test_chat_degrades_when_retrieve_fails(client, monkeypatch):
    monkeypatch.setattr(rag_router, "retrieve", AsyncMock(side_effect=RuntimeError("supabase caído")))
    gen = AsyncMock(return_value="Respuesta general.")
    monkeypatch.setattr(rag_router, "generate_reply", gen)

    resp = client.post("/rag/chat", json={"message": "hola", "history": []})

    assert resp.status_code == 200                       # no se cae por el fallo del RAG
    assert resp.json()["reply"] == "Respuesta general."
    assert gen.await_args.kwargs["context_chunks"] == []  # degradó sin contexto


def test_chat_rejects_empty_message(client):
    resp = client.post("/rag/chat", json={"message": "   ", "history": []})
    assert resp.status_code == 400
```

- [ ] **Step 2: Correr los tests para verlos fallar**

Run: `venv\Scripts\python -m pytest tests/test_rag_router.py -k chat -v`
Expected: `test_chat_passes_retrieved_context` y `test_chat_degrades_when_retrieve_fails` FALLAN (hoy `rag_chat` no llama `retrieve` ni pasa `context_chunks`). `test_chat_rejects_empty_message` ya pasa.

- [ ] **Step 3: Añadir el retrieval best-effort en `rag_chat`**

En `rag/router.py`, dentro de `rag_chat`, reemplazar el bloque actual:

```python
    history = [t.model_dump() for t in payload.history]
    try:
        reply = await generate_reply(request.app.state.http_client, message, history)
```

por:

```python
    history = [t.model_dump() for t in payload.history]

    client = request.app.state.http_client
    context_chunks: list[dict] = []
    try:
        context_chunks = await retrieve(client, message)
    except Exception as exc:  # noqa: BLE001 — el RAG es best-effort, nunca tumba el chat
        logger.warning("rag_chat: retrieve falló, se responde sin contexto: %s", exc)

    try:
        reply = await generate_reply(client, message, history, context_chunks=context_chunks)
```

(Los `except httpx.HTTPStatusError / RequestError / RuntimeError` de Gemini que siguen quedan igual, solo cambia la llamada a `generate_reply` para pasar `context_chunks`.)

- [ ] **Step 4: Correr los tests para verlos pasar**

Run: `venv\Scripts\python -m pytest tests/test_rag_router.py -k chat -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Correr la suite completa**

Run: `venv\Scripts\python -m pytest tests/ -q`
Expected: toda la suite verde (los nuevos + todos los existentes, incluidos los de `/rag/ask` que no se tocaron).

- [ ] **Step 6: Commit local (sin push)**

```bash
git add rag/router.py tests/test_rag_router.py
git commit -m "feat(rag): /rag/chat recupera contexto best-effort (Sharky ya usa el RAG)"
```

---

## Cierre (verificación en vivo, tras implementar)

Con el backend local levantado (ya corriendo en `http://127.0.0.1:8000`) y un JWT de prueba:
1. `POST /rag/chat` con "¿por qué volvió el mapa Cache?" → la respuesta debe reflejar las noticias reales (Cache reemplaza a Overpass), no conocimiento genérico.
2. `POST /rag/chat` con "hola" → responde normal, sin forzar noticias.
3. (Ojo con los 503 transitorios de Gemini free tier; reintentar si aparecen.)

---

## Self-Review (cobertura del spec)

- RAG-always: `retrieve` por mensaje en `/rag/chat` → Task 2 ✅
- Inyección de contexto en el turno actual + comportamiento actual si vacío → Task 1 ✅
- System prompt híbrido → Task 1 Step 3 ✅
- Best-effort: fallo de `retrieve` no tumba el chat → Task 2 (test de degradación) ✅
- Contrato `{reply}` sin fuentes → no se toca `ChatResponse` ✅
- Reutiliza `retrieve` y `_format_context` → Tasks 1-2 ✅
- Fuera de alcance (fuentes, function calling, retry 503, predicción) → no se toca ✅
