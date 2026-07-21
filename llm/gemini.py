"""Cliente Gemini puro (transporte REST). Sin lógica de negocio ni prompts.

Capa neutra compartida por rag/ (generación con contexto) y chat/ (agente con
tools). Usa el httpx.AsyncClient compartido de la app, sin SDK de Google.
"""
import json
import logging

import httpx

from settings import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger("uvicorn.error")

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# Timeout propio: el compartido de la app es 10 s, corto para un LLM.
_GEMINI_TIMEOUT = 30.0

# Umbral de aviso del tamaño del cuerpo. Medido contra gemini-flash-latest:
# ~7 KB responde en 3.6 s, ~20 KB devuelve 503 tras ~115 s. Como el payload se
# acumula vuelta a vuelta del loop de tools, cruzar esto suele acabar en un
# ReadTimeout mudo que parece un fallo de red y no lo es.
_BODY_WARN_CHARS = 15_000


async def call(client: httpx.AsyncClient, body: dict) -> dict:
    """POST a Gemini :generateContent. Devuelve el JSON de respuesta.

    Lanza RuntimeError si falta la API key; propaga los errores httpx (el router
    los traduce a HTTP). Nota: si Gemini devuelve 404, actualizar GEMINI_MODEL.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY no configurada")
    url = f"{_GEMINI_BASE}/{GEMINI_MODEL}:generateContent"

    size = len(json.dumps(body, ensure_ascii=False, default=str))
    if size > _BODY_WARN_CHARS:
        logger.warning(
            "[gemini] cuerpo de %d chars (umbral %d): Gemini puede tardar o "
            "devolver 503. Suele ser una tool de listado devolviendo de más.",
            size, _BODY_WARN_CHARS,
        )

    try:
        resp = await client.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY},
            json=body,
            timeout=_GEMINI_TIMEOUT,
        )
    except httpx.TimeoutException:
        # httpx da un mensaje vacío en estos timeouts: sin esto el router
        # reporta "no se pudo contactar" y el tamaño, que es la causa, se pierde.
        logger.warning(
            "[gemini] timeout tras %.0fs con cuerpo de %d chars", _GEMINI_TIMEOUT, size
        )
        raise
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
