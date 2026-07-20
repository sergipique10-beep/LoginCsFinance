# Sharky con RAG-always: conectar el retrieval al chat — Design

**Fecha:** 2026-07-20
**Rama:** `feat/rag-chat` (commits locales, sin push/merge)
**Estado:** aprobado, listo para plan de implementación

## Problema

Diagnóstico (systematic-debugging, 2026-07-20): **Sharky no usa el RAG.** El chat del
frontend llama `POST /rag/chat` → `generate_reply`, que es Gemini conversacional puro,
**sin retrieval**. El RAG (`retrieve` + pgvector + `generate_with_context`) existe solo
en `/rag/ask`, endpoint que el chat nunca invoca. Los dos caminos se construyeron
separados y quedaron desconectados. Resultado: Sharky responde de su memoria de
entrenamiento, sin ver las noticias ingestadas en `rag_chunks` (verificado en vivo:
`/rag/ask` respondió con datos exactos citando 5 noticias; `/rag/chat` no).

## Objetivo y alcance

**Objetivo:** que `/rag/chat` recupere contexto de las noticias (Supabase pgvector) en
**cada mensaje** y se lo inyecte a Gemini, manteniendo el historial conversacional.

**Enfoque elegido: RAG-always** (no function calling). Cada mensaje pasa por
`retrieve()`; el umbral de similitud (`RAG_MIN_SIMILARITY`=0.5) evita inyectar ruido.
Decidido con el usuario frente a la alternativa de function calling (más código, se
deja para cuando haya ≥2 tools, p.ej. junto a la predicción).

**En alcance:**
- Recuperación por mensaje en `/rag/chat`, best-effort (un fallo del RAG no tumba el chat).
- Inyección del contexto en el turno actual + system prompt híbrido.
- Tests TDD con red/DB mockeadas.

**Fuera de alcance (YAGNI):**
- Fuentes visibles en el chat (el contrato `ChatResponse` sigue siendo `{reply}`).
- Function calling / tool `buscar_contexto_rag` (enfoque descartado para v1).
- Retry/backoff ante los 503 transitorios de Gemini (mejora aparte).
- Tool de predicción de tendencia (aparcada por decisión del usuario).

## Contexto / datos

- El contexto sale de los **embeddings guardados en Supabase** (`rag_chunks`, 34 filas
  al momento del diseño), creados por la ingesta diaria (`/internal/rag-ingest`).
- `rag/retrieval.py:retrieve(client, query, k=5)` ya hace: embed de la query (Gemini)
  → `repo.match_chunks` (RPC `match_rag_chunks`, coseno) → filtra por
  `RAG_MIN_SIMILARITY`. Su docstring ya anticipa este reuso. Se reutiliza tal cual.

## Arquitectura

Cambios mínimos, sin unidades nuevas:

### 1. `rag/router.py` (`rag_chat`) — orquesta retrieve + generate

Igual que `/rag/ask` ya orquesta, pero **best-effort** en el retrieval:

```
message = payload.message.strip()
chunks = []
try:
    chunks = await retrieve(client, message)
except Exception:            # embedding/Supabase caído → degradar, no romper
    logger.warning(...)      # se sigue sin contexto
reply = await generate_reply(client, message, history, context_chunks=chunks)
```

El manejo de errores de Gemini (`HTTPStatusError`/`RequestError`/`RuntimeError`) del
bloque actual se mantiene. La diferencia clave con `/rag/ask`: ahí el fallo de
`retrieve` propaga (502/503); en el chat **degrada con gracia**.

### 2. `rag/gemini.py` (`generate_reply`) — parámetro opcional de contexto

Nueva firma: `generate_reply(client, message, history, context_chunks=None) -> str`.

- Construye `contents` desde el historial (igual que hoy).
- **Turno actual del usuario:**
  - Si `context_chunks` tiene elementos → el texto del turno es:
    ```
    CONTEXTO (noticias recientes; úsalo si es relevante, ignóralo si no aplica):
    <_format_context(chunks)>

    MENSAJE DEL USUARIO:
    <message>
    ```
    (reutiliza `_format_context`, ya existente en `gemini.py`).
  - Si `context_chunks` está vacío/None → el turno es solo `<message>` → **body
    idéntico al de hoy** (no cambia el comportamiento actual).
- El contexto va **solo en el turno actual**, no en el historial → no contamina turnos
  previos ni se re-inyecta en cada vuelta.

### 3. `_SYSTEM_PROMPT` (persona) — ajuste para modo híbrido

Añadir una frase para que use el contexto cuando aplique pero siga siendo
conversacional (no forzar "usa ÚNICAMENTE el contexto", que es de `/rag/ask` y
rompería un simple "hola"):

> "Cuando el CONTEXTO contenga información relevante, básate en ella para responder; si
> no aplica, responde con tu conocimiento general, sin inventar datos concretos."

## Flujo de datos

```
Usuario: "¿por qué volvió Cache?"
  → /rag/chat → retrieve(message)  [embed pregunta → match_rag_chunks → ≥0.5]
       ├─ chunks → generate_reply(msg, history, context_chunks=chunks)
       │     → Gemini: persona + historial + (CONTEXTO + mensaje) → respuesta fundada
       └─ 0 chunks / retrieve falla → generate_reply(msg, history) → chat normal
  → {reply}   (sin sources; contrato intacto)
```

## Manejo de errores (best-effort)

| Situación | Comportamiento |
|---|---|
| `retrieve` falla (embedding/Supabase) | capturado en router, log, sigue **sin contexto** |
| 0 chunks sobre el umbral | sin contexto → chat conversacional normal |
| Gemini 503/red | igual que hoy → 502 (retry fuera de alcance) |
| Gemini sin API key | igual que hoy → 503 "no configurado" |

## Testing (TDD, red/DB mockeadas)

- `generate_reply` **con** `context_chunks` → el body a Gemini incluye el bloque
  `CONTEXTO` + `MENSAJE DEL USUARIO` en el último turno.
- `generate_reply` **sin** contexto (None y `[]`) → último turno = solo el mensaje
  (sin bloque `CONTEXTO`): no rompe el comportamiento actual.
- `rag_chat` (router, TestClient): `retrieve` mockeado devolviendo chunks → se pasan a
  `generate_reply`.
- `rag_chat`: `retrieve` lanza excepción → responde **200** igualmente, con
  `generate_reply` invocado con `context_chunks` vacío (degradación).
- Suite completa verde antes de cada commit.

## Constraints operativos

- Commits locales en `feat/rag-chat`, mensaje `feat(rag): ...` terminado en
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Sin push/merge.
- Sin dependencias nuevas. Reutiliza `retrieve` y `_format_context`.
- `venv\Scripts\python -m pytest`. Archivos UTF-8.

## Camino futuro (no ahora)

Cuando se retome la tool de predicción, migrar de RAG-always a **function calling**:
`retrieve` pasa a ser la tool `buscar_contexto_rag` en el registro `_TOOLS`, junto a
`predecir_tendencia_skin`. Entonces Gemini decide con criterio cuál usar. RAG-always es
el puente honesto hasta que haya ≥2 tools que justifiquen el orquestador.
