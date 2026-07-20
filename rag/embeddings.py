"""Cliente mínimo de embeddings de Gemini (REST) para el RAG.

Reutiliza el httpx.AsyncClient compartido de la app, sin SDK de Google —
mismo patrón que llm/gemini.py. Produce vectores de 768 dims.
"""
import httpx

from settings import GEMINI_API_KEY, GEMINI_EMBED_MODEL

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_EMBED_TIMEOUT = 30.0
_EMBED_DIMS = 768


async def embed_text(client: httpx.AsyncClient, text: str) -> list[float]:
    """Devuelve el embedding (768 floats) del texto vía Gemini Embedding API.

    Lanza RuntimeError si falta la key o la respuesta viene vacía; propaga
    los errores httpx (el llamador los traduce).
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY no configurada")

    url = f"{_GEMINI_BASE}/{GEMINI_EMBED_MODEL}:embedContent"
    body = {
        "model": f"models/{GEMINI_EMBED_MODEL}",
        "content": {"parts": [{"text": text}]},
        "output_dimensionality": _EMBED_DIMS,
    }
    resp = await client.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json=body,
        timeout=_EMBED_TIMEOUT,
    )
    resp.raise_for_status()

    values = resp.json().get("embedding", {}).get("values", [])
    if not values:
        raise RuntimeError("Gemini devolvió un embedding vacío")
    return values
