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
