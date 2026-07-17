"""Cliente mínimo para el chat de Sharky vía Google Gemini (REST).

Usa la API REST de Google AI Studio directamente con el `httpx.AsyncClient`
compartido de la app — mismo patrón que las llamadas a Steam/steamwebapi, sin
añadir el SDK de Google como dependencia.

Fase 1: chat conversacional sin retrieval ni tools. La Fase 2 (agente con
function calling sobre precios/noticias + modelo de predicción) se construye
sobre esto.
"""

import logging

import httpx

from settings import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger("uvicorn.error")

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Timeout propio: el compartido de la app es 10 s, corto para un LLM.
_GEMINI_TIMEOUT = 30.0

_SYSTEM_PROMPT = (
    "Eres Sharky 🦈, el asistente de CS-FINANCE, experto en el mercado de "
    "skins de Counter-Strike 2 (precios, tendencias, liquidez, noticias). "
    "Respondes en español, de forma clara y concisa. Si no tienes datos "
    "suficientes para responder con certeza, dilo en lugar de inventar."
)


async def generate_reply(
    client: httpx.AsyncClient,
    message: str,
    history: list[dict],
) -> str:
    """Envía la conversación a Gemini y devuelve el texto de la respuesta.

    `history` son turnos previos `{"role": "user"|"assistant", "content": str}`.
    Lanza `RuntimeError` si falta la API key o la respuesta viene vacía/bloqueada,
    y propaga los errores httpx (el router los traduce a HTTP).
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
    }

    # Nota: si el modelo devuelve 404, actualiza GEMINI_MODEL en .env
    # (los nombres de modelo de Gemini cambian; p.ej. gemini-2.0-flash / 2.5-flash).
    url = f"{_GEMINI_BASE}/{GEMINI_MODEL}:generateContent"
    resp = await client.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json=body,
        timeout=_GEMINI_TIMEOUT,
    )
    resp.raise_for_status()

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        feedback = data.get("promptFeedback", {})
        logger.warning("Gemini sin candidates. promptFeedback=%s", feedback)
        raise RuntimeError("Gemini no devolvió respuesta (posible bloqueo de seguridad)")

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("Respuesta vacía de Gemini")
    return text


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


async def generate_with_context(
    client: httpx.AsyncClient,
    question: str,
    chunks: list[dict],
) -> str:
    """Genera una respuesta usando los chunks recuperados como contexto.

    Lanza RuntimeError si falta la key o la respuesta viene vacía/bloqueada;
    propaga los errores httpx.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY no configurada")

    prompt = (
        f"CONTEXTO:\n{_format_context(chunks)}\n\n"
        f"PREGUNTA DEL USUARIO:\n{question}"
    )
    body = {
        "systemInstruction": {"parts": [{"text": _RAG_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    url = f"{_GEMINI_BASE}/{GEMINI_MODEL}:generateContent"
    resp = await client.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json=body,
        timeout=_GEMINI_TIMEOUT,
    )
    resp.raise_for_status()

    candidates = resp.json().get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini no devolvió respuesta (posible bloqueo de seguridad)")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("Respuesta vacía de Gemini")
    return text
