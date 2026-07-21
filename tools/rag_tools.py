"""Tool de RAG sobre noticias CS2 para el orquestador de Sharky.

Reusa `rag.retrieval.retrieve` (embeddings Gemini + pgvector), que ya filtra por
RAG_MIN_SIMILARITY. Devuelve los chunks con su fuente para que el agente pueda
citar; si no hay nada por encima del umbral, lo dice explícitamente en vez de
dejar que el modelo rellene el hueco.
"""

from __future__ import annotations

import logging

import httpx

from tools.registry import register_tool

logger = logging.getLogger("uvicorn.error")

# Recorte por chunk. Alineado con chat.prompts._MAX_CHUNK_CHARS: el resultado de
# la tool se queda en `contents` para todas las vueltas siguientes, así que 5
# chunks de 1200 (6 KB) se pagan una y otra vez. Medido contra gemini-flash:
# ~7 KB de cuerpo responde en 3.6 s, ~20 KB devuelve 503 tras ~115 s.
_MAX_CHARS = 700
# Tope de fragmentos por defecto. Con 5 el modelo no respondía mejor, solo llenaba
# el contexto: los chunks vienen ya ordenados por similitud.
_DEFAULT_K = 3


async def _buscar_contexto_rag(
    *,
    query: str,
    client: httpx.AsyncClient,
    k: int = _DEFAULT_K,
    _sources_sink: list | None = None,
) -> dict:
    """Busca en las noticias CS2 indexadas los fragmentos relevantes a `query`.

    `_sources_sink` es un acumulador que pone el agente: los chunks recuperados
    por esta vía también deben acabar en el `sources[]` de la respuesta. Sin él,
    una respuesta documentada llegaba al frontend sin una sola fuente.
    """
    from rag.retrieval import retrieve

    try:
        chunks = await retrieve(client, query, k=k)
    except Exception as exc:  # noqa: BLE001 — best-effort: no romper la conversación
        logger.warning("[rag-tool] retrieve falló para %r: %s", query, exc)
        return {"encontrado": False, "motivo": f"error al buscar: {exc}", "fragmentos": []}

    if not chunks:
        return {
            "encontrado": False,
            "motivo": "sin noticias relevantes por encima del umbral de similitud",
            "fragmentos": [],
        }

    if _sources_sink is not None:
        _sources_sink.extend(chunks)

    return {
        "encontrado": True,
        "fragmentos": [
            {
                # Sin URL a propósito: el modelo las copia de memoria y cambia
                # dígitos (visto en E2E). Los enlaces viajan en `sources[]`.
                "contenido": (c.get("content") or "")[:_MAX_CHARS],
                "titulo": c.get("title"),
                "similitud": round(c.get("similarity", 0), 4),
            }
            for c in chunks
        ],
    }


def register_rag_tools() -> None:
    """Registra la tool de búsqueda en noticias en el registry."""
    register_tool(
        name="buscar_contexto_rag",
        description=(
            "Busca en las noticias y actualizaciones de CS2 indexadas los fragmentos "
            "relevantes a una consulta. Úsala para preguntas sobre novedades, parches, "
            "operaciones, cambios del juego o por qué algo pudo moverse de precio. "
            "Devuelve fragmentos con su título: cita las fuentes por el título "
            "(los enlaces los añade el cliente aparte, no los escribas tú). "
            "Si 'encontrado' es false, di que no hay información en las noticias — "
            "NO inventes ni respondas de memoria."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Consulta en lenguaje natural sobre noticias/actualizaciones de CS2",
                },
                "k": {
                    "type": "integer",
                    "description": "Número máximo de fragmentos a recuperar. Por defecto 5.",
                },
            },
            "required": ["query"],
        },
        fn=_buscar_contexto_rag,
    )
