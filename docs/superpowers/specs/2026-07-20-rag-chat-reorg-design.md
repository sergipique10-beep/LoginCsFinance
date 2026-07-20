# Reorganizar rag/ en llm/ + rag/ + chat/ — Design

**Fecha:** 2026-07-20
**Rama:** `feat/rag-chat` (commits locales, sin push/merge)
**Estado:** aprobado, listo para plan de implementación

## Problema

El paquete `rag/` mezcla dos responsabilidades distintas:
- **Base de conocimiento RAG** (retrieval de noticias): `embeddings.py`, `ingest.py`,
  `repo.py`, `retrieval.py`.
- **Asistente/LLM (Sharky)**: `gemini.py` (cliente Gemini + generación + function
  calling) y la parte `/rag/chat` de `router.py`.

Además `gemini.py` mezcla, a su vez, transporte puro del LLM, generación-RAG
(`generate_with_context`) y generación-chat (`generate_with_tools`). Ya existe un
paquete `chat/` **vacío** — señal de una separación empezada y no terminada. `tools/`
y `predict/` ya están separados; `rag/` se quedó atrás.

## Objetivo y alcance

**Objetivo:** separar en tres capas con dependencias limpias, **sin cambiar
comportamiento** (mismas URLs, misma lógica). Refactor puro de organización.

**En alcance:**
- Nueva capa `llm/` (cliente Gemini puro), compartida por rag y chat.
- `rag/` queda como base de conocimiento + generación-RAG.
- `chat/` (hoy vacío) se llena con el asistente Sharky (agente + function calling).
- Eliminar código muerto: `generate_reply` + `_build_user_text` (enfoque RAG-always
  superado por el function calling; solo lo usan sus propios tests).
- Reorganizar tests en paralelo al código.

**Fuera de alcance (YAGNI):**
- Renombrar URLs (se mantienen `/rag/chat`, `/rag/ask`, `/internal/rag-ingest` →
  cero cambios en el frontend).
- Cambios de comportamiento, nuevos endpoints, nuevas tools (p.ej. `buscar_contexto_rag`).
- Tocar `tools/`, `predict/`, `steam/`, `auth/`, etc.

## Decisiones (tomadas con el usuario)

1. **Cliente Gemini → capa `llm/` compartida** (no dentro de `chat/`), para que ni
   `rag/` dependa de `chat/` ni al revés. Ambos cuelgan de `llm/`.
2. **URLs sin cambios** — la reorganización es interna.

## Estructura objetivo

```
llm/
  __init__.py
  gemini.py        transporte puro: constantes (_GEMINI_BASE, _GEMINI_TIMEOUT),
                   call(client, body) -> dict (POST a :generateContent + raise +
                   json), _extract_text, _extract_function_call, chequeo API key
rag/
  embeddings.py, ingest.py, repo.py, retrieval.py    (sin cambios)
  generation.py    _RAG_SYSTEM_PROMPT, _format_context, generate_with_context (usa llm)
  router.py        /rag/ask + /internal/rag-ingest
chat/
  __init__.py
  prompts.py       _SYSTEM_PROMPT_TOOLS
  agent.py         generate_with_tools (usa llm + tools)
  router.py        /rag/chat + registro de tools (market/inventory/predict)
tools/  predict/   (sin cambios)
```

## Mapeo viejo → nuevo

De `rag/gemini.py`:
| Símbolo | Destino |
|---|---|
| `_GEMINI_BASE`, `_GEMINI_TIMEOUT`, POST a Gemini, `_extract_text`→`extract_text`, `_extract_function_call`→`extract_function_call`, chequeo `GEMINI_API_KEY` | `llm/gemini.py` |
| `_RAG_SYSTEM_PROMPT`, `_format_context`, `generate_with_context` | `rag/generation.py` |
| `_SYSTEM_PROMPT_TOOLS`, `generate_with_tools` | `chat/agent.py` |
| `generate_reply`, `_build_user_text`, `_SYSTEM_PROMPT` | eliminar (código muerto) |

De `rag/router.py`:
| Rutas | Destino |
|---|---|
| `/rag/chat` + registro de tools | `chat/router.py` |
| `/rag/ask`, `/internal/rag-ingest` | `rag/router.py` (queda) |

`main.py`: registrar `chat_router` además de `rag_router`.

## Interfaces de la capa `llm/`

```python
# llm/gemini.py — API pública de la capa (sin guion bajo: la usan rag y chat)
async def call(client: httpx.AsyncClient, body: dict) -> dict:
    """POST a Gemini :generateContent. Lanza RuntimeError si falta la API key;
    propaga errores httpx; devuelve el JSON de respuesta."""

def extract_text(candidates: list[dict]) -> str | None: ...
def extract_function_call(candidates: list[dict]) -> dict | None: ...
```

`generate_with_context` (rag) y `generate_with_tools` (chat) construyen su `body`
(con su propio system prompt y, en chat, `tools`) y llaman a `llm.call`, luego usan
`extract_text`/`extract_function_call` para parsear. (Los `_extract_*` actuales, hoy
privados en `rag/gemini.py`, pasan a públicos como API de `llm/`.)

## Dependencias resultantes (sin cruces)

```
llm/  ◄── rag/generation ◄── rag/router
  ▲
  └────── chat/agent ──► tools/ ──► predict/
             ▲
          chat/router
   main ──► rag/router + chat/router
```

`rag/` no importa `chat/` ni viceversa.

## Reorganización de tests

| Test hoy | Acción |
|---|---|
| `tests/test_gemini_tools.py` (generate_with_tools) | → `tests/test_chat_agent.py` (import de `chat.agent`) |
| `tests/test_rag_gemini.py` :: `test_generate_with_context_*` | → `tests/test_rag_generation.py` (import de `rag.generation`) |
| `tests/test_rag_gemini.py` :: `test_generate_reply_*` | eliminar (código muerto) |
| `tests/test_rag_router.py` :: `test_chat_*` | → `tests/test_chat_router.py` |
| `tests/test_rag_router.py` :: `/rag/ask` + `rag_ingest` | quedan |
| `_extract_*` (si se testean) | contra `llm.gemini` |

## Constraints operativos

- Commits locales en `feat/rag-chat`, mensaje `refactor(rag): ...` /
  `refactor(chat): ...`, cuerpo con `Co-Authored-By: Claude Opus 4.8
  <noreply@anthropic.com>`. Sin push/merge.
- **Comportamiento idéntico**: mismas URLs, misma lógica. La suite completa
  (`venv\Scripts\python -m pytest`) debe quedar verde tras cada movimiento —
  es la red de seguridad del refactor.
- Sin dependencias nuevas. Archivos UTF-8.
- Actualizar imports en todos los consumidores (`main.py`, `tools/`, tests).

## Verificación

- Suite completa verde tras el refactor (mismos tests, salvo los muertos eliminados).
- Levantar el backend local: startup sin errores, `/rag/ask`, `/rag/chat`,
  `/internal/rag-ingest` responden igual que antes (mismas URLs).
