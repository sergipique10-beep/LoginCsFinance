# Reorganizar rag/ en llm/ + rag/ + chat/ — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separar el paquete `rag/` en tres capas con dependencias limpias — `llm/` (cliente Gemini puro), `rag/` (base de conocimiento + generación-RAG) y `chat/` (asistente Sharky) — sin cambiar comportamiento ni URLs.

**Architecture:** `gemini.py` se parte: transporte → `llm/gemini.py`; generación-RAG (`generate_with_context`) → `rag/generation.py`; agente (`generate_with_tools`) → `chat/agent.py`. El `/rag/chat` y el registro de tools pasan a `chat/router.py`; `/rag/ask` + `/internal/rag-ingest` quedan en `rag/router.py`. Se elimina código muerto (`generate_reply`, `_build_user_text`). Refactor puro: la suite verde es la red de seguridad.

**Tech Stack:** Python 3 / FastAPI, httpx, Gemini REST, pytest + pytest-asyncio.

## Global Constraints

- **Commits locales SÍ; push y merge NO.** Cada tarea termina en commit local en `feat/rag-chat`. Mensaje `refactor(rag): ...` / `refactor(chat): ...`, cuerpo terminado en `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Nunca `git push`/`git merge`.
- **Comportamiento idéntico**: mismas URLs (`/rag/chat`, `/rag/ask`, `/internal/rag-ingest`), misma lógica. Correr la suite completa (`venv\Scripts\python -m pytest`) al final de cada tarea; debe quedar verde.
- Sin dependencias nuevas. Archivos nuevos en UTF-8.
- Los `_extract_*` privados de `rag/gemini.py` pasan a públicos (`extract_text`, `extract_function_call`) como API de `llm/`.

## File Structure

- `llm/__init__.py`, `llm/gemini.py` — **crear**. Transporte + parseo.
- `rag/generation.py` — **crear**. `generate_with_context` + `_format_context` + `_RAG_SYSTEM_PROMPT`.
- `chat/__init__.py`, `chat/prompts.py`, `chat/agent.py`, `chat/router.py` — **crear**.
- `rag/router.py` — **modificar**. Quitar `/rag/chat` + registro de tools; usar `rag.generation`.
- `rag/gemini.py` — **eliminar** (al final, cuando quede sin uso).
- `main.py` — **modificar**. Registrar `chat_router`.
- `tests/test_llm_gemini.py`, `tests/test_rag_generation.py`, `tests/test_chat_agent.py`, `tests/test_chat_router.py` — **crear** (mover tests).
- `tests/test_gemini_tools.py` — **eliminar** (movido a `test_chat_agent.py`).
- `tests/test_rag_gemini.py` — **eliminar** (parte movida, parte borrada por código muerto).

---

## Task 1: Capa `llm/` (cliente Gemini puro)

**Files:**
- Create: `llm/__init__.py` (vacío)
- Create: `llm/gemini.py`
- Test: `tests/test_llm_gemini.py`

**Interfaces:**
- Produces: `async def call(client, body) -> dict`; `def extract_text(candidates) -> str | None`; `def extract_function_call(candidates) -> dict | None`. Constantes `_GEMINI_BASE`, `_GEMINI_TIMEOUT`. Lee `GEMINI_API_KEY`, `GEMINI_MODEL` de settings.

- [ ] **Step 1: Crear el paquete y el módulo**

Crear `llm/__init__.py` vacío. Crear `llm/gemini.py`:

```python
"""Cliente Gemini puro (transporte REST). Sin lógica de negocio ni prompts.

Capa neutra compartida por rag/ (generación con contexto) y chat/ (agente con
tools). Usa el httpx.AsyncClient compartido de la app, sin SDK de Google.
"""
import logging

import httpx

from settings import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger("uvicorn.error")

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# Timeout propio: el compartido de la app es 10 s, corto para un LLM.
_GEMINI_TIMEOUT = 30.0


async def call(client: httpx.AsyncClient, body: dict) -> dict:
    """POST a Gemini :generateContent. Devuelve el JSON de respuesta.

    Lanza RuntimeError si falta la API key; propaga los errores httpx (el router
    los traduce a HTTP). Nota: si Gemini devuelve 404, actualizar GEMINI_MODEL.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY no configurada")
    url = f"{_GEMINI_BASE}/{GEMINI_MODEL}:generateContent"
    resp = await client.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json=body,
        timeout=_GEMINI_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def extract_text(candidates: list[dict]) -> str | None:
    """Texto concatenado del primer candidate (o None si no hay)."""
    if not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    return text or None


def extract_function_call(candidates: list[dict]) -> dict | None:
    """El part completo con functionCall (incluye thoughtSignature si existe)."""
    if not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    for part in parts:
        if "functionCall" in part:
            return part
    return None
```

- [ ] **Step 2: Escribir el test**

Crear `tests/test_llm_gemini.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from llm import gemini


@pytest.mark.asyncio
async def test_call_posts_and_returns_json(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(gemini, "GEMINI_MODEL", "gemini-flash-latest")
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)

    data = await gemini.call(client, {"contents": []})

    assert data["candidates"][0]["content"]["parts"][0]["text"] == "hi"
    assert "generateContent" in client.post.call_args.args[0]


@pytest.mark.asyncio
async def test_call_raises_without_key(monkeypatch):
    monkeypatch.setattr(gemini, "GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError):
        await gemini.call(MagicMock(), {})


def test_extract_text():
    assert gemini.extract_text([{"content": {"parts": [{"text": "hola"}]}}]) == "hola"
    assert gemini.extract_text([]) is None
    assert gemini.extract_text([{"content": {"parts": [{"functionCall": {}}]}}]) is None


def test_extract_function_call():
    fc = {"functionCall": {"name": "x", "args": {}}}
    assert gemini.extract_function_call([{"content": {"parts": [fc]}}]) == fc
    assert gemini.extract_function_call([]) is None
    assert gemini.extract_function_call([{"content": {"parts": [{"text": "hi"}]}}]) is None
```

- [ ] **Step 3: Correr los tests**

Run: `venv\Scripts\python -m pytest tests/test_llm_gemini.py -v`
Expected: PASS (4 passed).

- [ ] **Step 4: Suite completa (nada roto, módulo nuevo aislado)**

Run: `venv\Scripts\python -m pytest tests/ -q`
Expected: verde (todo lo anterior + los 4 nuevos).

- [ ] **Step 5: Commit local (sin push)**

```bash
git add llm/__init__.py llm/gemini.py tests/test_llm_gemini.py
git commit -m "refactor(rag): capa llm/ — cliente Gemini puro (call + extract_*)"
```

---

## Task 2: Generación-RAG → `rag/generation.py`

**Files:**
- Create: `rag/generation.py`
- Modify: `rag/router.py` (import de `generate_with_context`)
- Create: `tests/test_rag_generation.py` (mover los tests de `generate_with_context`)
- Modify: `tests/test_rag_gemini.py` (quitar los tests movidos)

**Interfaces:**
- Consumes: `llm.gemini.call`, `llm.gemini.extract_text`.
- Produces: `async def generate_with_context(client, question, chunks) -> str`; `def _format_context(chunks) -> str`.

- [ ] **Step 1: Crear `rag/generation.py`**

```python
"""Generación de respuestas RAG: arma el prompt con el contexto recuperado y
llama al LLM. Usada por /rag/ask. La recuperación vive en rag/retrieval.py.
"""
from llm.gemini import call, extract_text

_RAG_SYSTEM_PROMPT = (
    "Eres Sharky 🦈, asistente de CS-FINANCE experto en el mercado de skins de "
    "Counter-Strike 2. Respondes en español, claro y conciso. Usa ÚNICAMENTE la "
    "información del CONTEXTO para responder. Si el contexto no contiene la "
    "respuesta, dilo explícitamente en vez de inventar."
)


def _format_context(chunks: list[dict]) -> str:
    if not chunks:
        return "(No hay noticias relevantes en la base.)"
    blocks = []
    for c in chunks:
        title = c.get("title") or "(sin título)"
        url = c.get("url") or ""
        content = c.get("content") or ""
        blocks.append(f"### {title}\n{content}\nFuente: {url}")
    return "\n\n".join(blocks)


async def generate_with_context(client, question: str, chunks: list[dict]) -> str:
    """Genera una respuesta usando los chunks recuperados como contexto.

    Lanza RuntimeError si falta la key o la respuesta viene vacía/bloqueada;
    propaga los errores httpx.
    """
    prompt = (
        f"CONTEXTO:\n{_format_context(chunks)}\n\n"
        f"PREGUNTA DEL USUARIO:\n{question}"
    )
    body = {
        "systemInstruction": {"parts": [{"text": _RAG_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    data = await call(client, body)
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini no devolvió respuesta (posible bloqueo de seguridad)")
    text = extract_text(candidates)
    if not text:
        raise RuntimeError("Respuesta vacía de Gemini")
    return text
```

- [ ] **Step 2: Actualizar el import en `rag/router.py`**

Cambiar la línea 11 actual:
```python
from .gemini import generate_reply, generate_with_context, generate_with_tools
```
por:
```python
from .generation import generate_with_context
```
(El `/rag/ask` sigue igual; `generate_reply`/`generate_with_tools` ya no se importan aquí — el `/rag/chat` se moverá en la Task 3, de momento sigue usando el símbolo viejo, así que **este paso se hace junto con la Task 3**. Ver nota abajo.)

> **Nota de orden:** para no dejar `rag/router.py` roto entre tareas, en la Task 2 dejamos el import viejo de `rag/gemini` intacto para `/rag/chat` y **añadimos** `from .generation import generate_with_context`, quitando solo `generate_with_context` del import viejo:
> ```python
> from .gemini import generate_reply, generate_with_tools   # /rag/chat aún aquí
> from .generation import generate_with_context             # /rag/ask ya desde generation
> ```

- [ ] **Step 3: Mover los tests de `generate_with_context`**

Crear `tests/test_rag_generation.py` con los dos tests que hoy están en `tests/test_rag_gemini.py` (`test_generate_with_context_includes_chunks`, `test_generate_with_context_raises_without_key`), cambiando el import `from rag import gemini` por `from rag import generation` y las referencias `gemini.generate_with_context`→`generation.generate_with_context`. El monkeypatch de la API key ahora apunta a la capa llm:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from rag import generation
from llm import gemini as llm_gemini


@pytest.mark.asyncio
async def test_generate_with_context_includes_chunks(monkeypatch):
    monkeypatch.setattr(llm_gemini, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(llm_gemini, "GEMINI_MODEL", "gemini-flash-latest")

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "candidates": [{"content": {"parts": [{"text": "Subió por la operación."}]}}]
    })
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)

    chunks = [{"title": "Op nueva", "url": "u", "content": "Nueva operación en CS2."}]
    out = await generation.generate_with_context(client, "por que sube?", chunks)

    assert out == "Subió por la operación."
    body = client.post.call_args.kwargs["json"]
    assert "Nueva operación en CS2." in str(body)


@pytest.mark.asyncio
async def test_generate_with_context_raises_without_key(monkeypatch):
    monkeypatch.setattr(llm_gemini, "GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError):
        await generation.generate_with_context(MagicMock(), "x", [])
```

- [ ] **Step 4: Quitar los tests movidos de `tests/test_rag_gemini.py`**

Borrar de `tests/test_rag_gemini.py` los dos `test_generate_with_context_*` (quedan solo los `test_generate_reply_*`, que se eliminarán en la Task 4).

- [ ] **Step 5: Suite completa**

Run: `venv\Scripts\python -m pytest tests/ -q`
Expected: verde. `/rag/ask` ahora usa `rag.generation`; `/rag/chat` sigue usando `rag.gemini` (intacto).

- [ ] **Step 6: Commit local (sin push)**

```bash
git add rag/generation.py rag/router.py tests/test_rag_generation.py tests/test_rag_gemini.py
git commit -m "refactor(rag): generate_with_context a rag/generation.py (usa llm/)"
```

---

## Task 3: Asistente → `chat/` (agente + router)

**Files:**
- Create: `chat/__init__.py` (vacío), `chat/prompts.py`, `chat/agent.py`, `chat/router.py`
- Modify: `rag/router.py` (quitar `/rag/chat` + registro de tools + import de `rag.gemini`)
- Modify: `main.py` (registrar `chat_router`)
- Create: `tests/test_chat_agent.py` (mover `test_gemini_tools.py`), `tests/test_chat_router.py` (mover los `test_chat_*` de `test_rag_router.py`)
- Delete: `tests/test_gemini_tools.py`
- Modify: `tests/test_rag_router.py` (quitar los `test_chat_*`)

**Interfaces:**
- Consumes: `llm.gemini.call/extract_text/extract_function_call`, `tools.registry.execute_tool` y `get_declarations`, `tools.*_tools.register_*`.
- Produces: `chat/agent.py::generate_with_tools(client, message, history, tools=None, tool_context=None) -> str`; `chat/router.py::router` con `POST /rag/chat`.

- [ ] **Step 1: Crear `chat/prompts.py`**

```python
"""Prompts del asistente Sharky (chat con function calling)."""

_SYSTEM_PROMPT_TOOLS = (
    "Eres Sharky 🦈, el asistente de CS-FINANCE, experto en el mercado de "
    "skins de Counter-Strike 2 (precios, tendencias, liquidez, inventario, "
    "noticias). Respondes en español, de forma clara y concisa.\n\n"
    "Tienes acceso a herramientas para consultar datos reales del mercado. "
    "Úsalas cuando el usuario pregunte por precios, tendencias, inventario "
    "u otras datos concretos. Si no tienes datos suficientes para responder "
    "con certeza, dilo en lugar de inventar."
)
```

- [ ] **Step 2: Crear `chat/agent.py`**

Mover `generate_with_tools` desde `rag/gemini.py` **verbatim**, cambiando: (a) el body/POST para usar `llm.gemini.call`; (b) el parseo para usar `extract_text`/`extract_function_call`; (c) el system prompt importado de `chat.prompts`. Contenido:

```python
"""Agente Sharky: chat con function calling sobre Gemini.

Un turno de tool por mensaje. El transporte al LLM vive en llm/; las tools en
tools/. tool_context lleva datos ocultos a Gemini (ej. steam_id).
"""
import json
import logging

import httpx

from llm.gemini import call, extract_text, extract_function_call
from chat.prompts import _SYSTEM_PROMPT_TOOLS

logger = logging.getLogger("uvicorn.error")


async def generate_with_tools(
    client: httpx.AsyncClient,
    message: str,
    history: list[dict],
    tools: list[dict] | None = None,
    tool_context: dict | None = None,
) -> str:
    """Genera una respuesta con soporte de function calling.

    1. Llamada con tools declaradas. 2. Si hay texto → devolver. 3. Si hay
    functionCall → ejecutar la tool → 2ª llamada con functionResponse → texto.
    """
    contents: list[dict] = []
    for turn in history:
        text = (turn.get("content") or "").strip()
        if not text:
            continue
        role = "model" if turn.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    body: dict = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT_TOOLS}]},
        "contents": contents,
    }
    if tools:
        body["tools"] = [{"functionDeclarations": tools}]

    data = await call(client, body)
    candidates = data.get("candidates", [])
    if not candidates:
        feedback = data.get("promptFeedback", {})
        logger.warning("Gemini sin candidates. promptFeedback=%s", feedback)
        raise RuntimeError("Gemini no devolvió respuesta (posible bloqueo de seguridad)")

    text = extract_text(candidates)
    if text:
        return text

    fc_part = extract_function_call(candidates)
    if not fc_part:
        raise RuntimeError("Respuesta vacía de Gemini (sin texto ni functionCall)")

    fc_call = fc_part["functionCall"]
    fn_name = fc_call.get("name", "")
    fn_args = fc_call.get("args", {})
    logger.info("[chat-agent] functionCall: %s(%s)", fn_name, fn_args)

    from tools.registry import execute_tool
    try:
        ctx = tool_context or {}
        result = await execute_tool(fn_name, steam_id=ctx.get("steam_id"),
                                    client=client, **fn_args)
    except (KeyError, ValueError) as exc:
        logger.warning("[chat-agent] tool '%s' falló: %s", fn_name, exc)
        return f"No pude ejecutar la herramienta '{fn_name}': {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[chat-agent] error ejecutando '%s': %s", fn_name, exc)
        return f"Error al ejecutar '{fn_name}': {exc}"

    result_str = json.dumps(result, ensure_ascii=False, default=str)
    contents.append({"role": "model", "parts": [fc_part]})
    contents.append({"role": "user", "parts": [
        {"functionResponse": {"name": fn_name, "response": {"result": result_str}}}]})

    data2 = await call(client, body)
    text2 = extract_text(data2.get("candidates", []))
    if text2:
        return text2
    raise RuntimeError("Respuesta vacía de Gemini tras la tool")
```

(Nota: se mantiene el `body` compartido — al reusarlo en la 2ª llamada, `body["contents"]` es la misma lista que se acaba de extender, igual que en el original.)

- [ ] **Step 3: Crear `chat/router.py`**

Mover el `/rag/chat` y el registro de tools desde `rag/router.py`:

```python
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth.service import require_jwt, _get_client_ip, _rate_limit
from chat.agent import generate_with_tools

from tools.registry import get_declarations
from tools.market_tools import register_market_tools
from tools.inventory_tools import register_inventory_tools
from tools.predict_tools import register_predict_tools

register_market_tools()
register_inventory_tools()
register_predict_tools()

logger = logging.getLogger("uvicorn.error")

router = APIRouter()


class ChatTurn(BaseModel):
    role: str
    content: str = ""


class ChatRequest(BaseModel):
    message: str
    history: list[ChatTurn] = []


class ChatResponse(BaseModel):
    reply: str


@router.post("/rag/chat", response_model=ChatResponse, summary="Chat con Sharky (Gemini)")
async def rag_chat(
    payload: ChatRequest,
    request: Request,
    _claims: dict = Depends(require_jwt),
):
    _rate_limit(_get_client_ip(request))

    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="El mensaje está vacío")

    history = [t.model_dump() for t in payload.history]
    steam_id: str = _claims["sub"]
    tools = get_declarations()

    try:
        reply = await generate_with_tools(
            request.app.state.http_client, message, history,
            tools=tools if tools else None,
            tool_context={"steam_id": steam_id},
        )
    except httpx.HTTPStatusError as exc:
        logger.warning("Gemini devolvió %s: %s", exc.response.status_code, exc.response.text[:300])
        raise HTTPException(status_code=502, detail="El asistente no está disponible ahora mismo")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar con el asistente: {exc}")
    except RuntimeError as exc:
        logger.warning("rag_chat: %s", exc)
        raise HTTPException(status_code=503, detail="El asistente no está configurado")

    return ChatResponse(reply=reply)
```

- [ ] **Step 4: Limpiar `rag/router.py`**

Quitar de `rag/router.py`: el bloque de imports/registro de tools (líneas ~14-22), la clase `ChatTurn`/`ChatRequest`/`ChatResponse`, la ruta `rag_chat` completa, y los imports `from .gemini import ...` y `from .generation import ...` reajustados a solo lo que usa `/rag/ask`:
```python
from .retrieval import retrieve
from .generation import generate_with_context
from .ingest import ingest
```
Queda `rag/router.py` con `/rag/ask` + `/internal/rag-ingest` únicamente.

- [ ] **Step 5: Registrar `chat_router` en `main.py`**

Añadir junto a los otros routers:
```python
from chat.router import router as chat_router
```
y tras `app.include_router(rag_router)`:
```python
app.include_router(chat_router)
```

- [ ] **Step 6: Mover los tests del agente**

Crear `tests/test_chat_agent.py` con el contenido de `tests/test_gemini_tools.py`, cambiando: `from rag import gemini` → `from chat import agent` y `from llm import gemini as llm_gemini`; todas las llamadas `gemini.generate_with_tools` → `agent.generate_with_tools`; y los monkeypatch de `gemini.GEMINI_API_KEY`/`GEMINI_MODEL` → `llm_gemini.GEMINI_API_KEY`/`GEMINI_MODEL` (el fixture y `test_sin_api_key_raises`). Los `patch("tools.registry.execute_tool", ...)` quedan igual. Luego borrar `tests/test_gemini_tools.py`.

- [ ] **Step 7: Mover los tests del router de chat**

Crear `tests/test_chat_router.py` con los `test_chat_*` que hoy están en `tests/test_rag_router.py`, cambiando el import `from rag import router as rag_router` por `from chat import router as chat_router` y los `monkeypatch.setattr(rag_router, "generate_reply", ...)` / `retrieve` por los símbolos que use el chat. **Ojo:** los tests actuales de `/rag/chat` mockeaban `rag_router.retrieve`/`generate_reply` (del enfoque RAG-always eliminado). Reescribirlos para el agente:

```python
from unittest.mock import AsyncMock

from chat import router as chat_router


def test_chat_returns_reply(client, monkeypatch):
    monkeypatch.setattr(chat_router, "generate_with_tools",
                        AsyncMock(return_value="Hola, soy Sharky."))
    resp = client.post("/rag/chat", json={"message": "hola", "history": []})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "Hola, soy Sharky."


def test_chat_rejects_empty_message(client):
    resp = client.post("/rag/chat", json={"message": "   ", "history": []})
    assert resp.status_code == 400
```

Quitar los `test_chat_*` viejos de `tests/test_rag_router.py`.

- [ ] **Step 8: Suite completa**

Run: `venv\Scripts\python -m pytest tests/ -q`
Expected: verde. `/rag/chat` servido por `chat/router`, `/rag/ask` por `rag/router`, ambos con las URLs de siempre.

- [ ] **Step 9: Commit local (sin push)**

```bash
git add chat/ rag/router.py main.py tests/test_chat_agent.py tests/test_chat_router.py tests/test_rag_router.py
git rm tests/test_gemini_tools.py
git commit -m "refactor(chat): asistente Sharky a chat/ (agent + router); URLs intactas"
```

---

## Task 4: Eliminar `rag/gemini.py` (código muerto)

**Files:**
- Delete: `rag/gemini.py`
- Delete: `tests/test_rag_gemini.py` (solo quedan los `test_generate_reply_*`, código muerto)

**Interfaces:** ninguna nueva; se verifica que nada importe `rag.gemini`.

- [ ] **Step 1: Verificar que `rag/gemini.py` ya no se usa**

Run: `grep -rn "rag.gemini\|from .gemini\|from rag import gemini" --include="*.py" . | grep -v venv`
Expected: sin resultados (todo migrado a `llm/`, `rag/generation`, `chat/agent`).

- [ ] **Step 2: Borrar el módulo muerto y su test**

```bash
git rm rag/gemini.py tests/test_rag_gemini.py
```

(`rag/gemini.py` ya solo contenía `generate_reply`, `_build_user_text`, `_SYSTEM_PROMPT` — código muerto; el resto se movió. `tests/test_rag_gemini.py` ya solo tenía los `test_generate_reply_*`.)

- [ ] **Step 3: Suite completa**

Run: `venv\Scripts\python -m pytest tests/ -q`
Expected: verde, sin referencias colgando.

- [ ] **Step 4: Verificación en vivo (arranque + rutas)**

Run: `venv\Scripts\python -c "import main; print('import ok')"`
Expected: `import ok` (sin ImportError por el borrado).

- [ ] **Step 5: Commit local (sin push)**

```bash
git commit -m "refactor(rag): eliminar rag/gemini.py (código muerto tras la reorg)"
```

---

## Cierre (verificación en vivo, tras el refactor)

Con el backend local (`uvicorn main:app --reload` ya recarga): confirmar que
`/rag/ask`, `/rag/chat` y `/internal/rag-ingest` responden en las **mismas URLs**
que antes. (La generación de Gemini puede estar limitada por cuota; el objetivo es
que el arranque y el ruteo funcionen — el comportamiento no cambió.)

---

## Self-Review (cobertura del spec)

- Capa `llm/` (call + extract_*) → Task 1 ✅
- `rag/generation.py` (generate_with_context) usa llm → Task 2 ✅
- `chat/agent.py` (generate_with_tools) + `chat/prompts.py` usan llm+tools → Task 3 ✅
- `/rag/chat` + registro de tools a `chat/router.py`; `/rag/ask`+ingest en `rag/router.py` → Task 3 ✅
- `main.py` registra ambos routers → Task 3 ✅
- Eliminar código muerto (`generate_reply`, `_build_user_text`, `rag/gemini.py`) → Task 4 ✅
- Tests reorganizados en paralelo → Tasks 1-4 ✅
- URLs sin cambios, sin dependencias cruzadas rag↔chat → Tasks 2-3 ✅
- Suite verde tras cada tarea → Steps de cada tarea ✅
