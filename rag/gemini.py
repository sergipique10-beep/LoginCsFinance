"""Cliente mínimo para el chat de Sharky vía Google Gemini (REST).

Usa la API REST de Google AI Studio directamente con el `httpx.AsyncClient`
compartido de la app — mismo patrón que las llamadas a Steam/steamwebapi, sin
añadir el SDK de Google como dependencia.

Soporta dos modos:
  - ``generate_reply``: chat conversacional sin tools (Fase 1)
  - ``generate_with_tools``: agente con function calling (Fase 2)
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
    "Respondes en español, de forma clara y concisa. "
    "Cuando el CONTEXTO contenga información relevante, básate en ella para "
    "responder; si no aplica, responde con tu conocimiento general. "
    "Si no tienes datos suficientes para responder con certeza, dilo en lugar "
    "de inventar."
)


def _build_user_text(message: str, context_chunks: list[dict] | None) -> str:
    """Turno del usuario: con contexto RAG inyectado si lo hay, si no el mensaje pelado."""
    if context_chunks:
        return (
            "CONTEXTO (noticias recientes; úsalo si es relevante, ignóralo si no aplica):\n"
            f"{_format_context(context_chunks)}\n\n"
            f"MENSAJE DEL USUARIO:\n{message}"
        )
    return message


async def generate_reply(
    client: httpx.AsyncClient,
    message: str,
    history: list[dict],
    context_chunks: list[dict] | None = None,
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
    contents.append({"role": "user", "parts": [{"text": _build_user_text(message, context_chunks)}]})

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


# ── Function calling (Fase 2) ────────────────────────────────────────────────

_SYSTEM_PROMPT_TOOLS = (
    "Eres Sharky 🦈, el asistente de CS-FINANCE, experto en el mercado de "
    "skins de Counter-Strike 2 (precios, tendencias, liquidez, inventario, "
    "noticias). Respondes en español, de forma clara y concisa.\n\n"
    "Tienes acceso a herramientas para consultar datos reales del mercado. "
    "Úsalas cuando el usuario pregunte por precios, tendencias, inventario "
    "u otras datos concretos. Si no tienes datos suficientes para responder "
    "con certeza, dilo en lugar de inventar."
)


def _extract_text(candidates: list[dict]) -> str | None:
    """Extrae el texto de los candidates de Gemini (si existe)."""
    if not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    return text or None


def _extract_function_call(candidates: list[dict]) -> dict | None:
    """Extrae un functionCall del primer candidate (si existe).

    Retorna el **part completo** (incluyendo ``thoughtSignature`` si existe)
    para poder reenviarlo tal cual en la segunda llamada.
    """
    if not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    for part in parts:
        if "functionCall" in part:
            return part
    return None


async def generate_with_tools(
    client: httpx.AsyncClient,
    message: str,
    history: list[dict],
    tools: list[dict] | None = None,
    tool_context: dict | None = None,
) -> str:
    """Genera una respuesta con soporte de function calling.

    Flujo:
      1. Primera llamada a Gemini con ``tools`` declaradas.
      2. Si Gemini responde con texto → devolver directamente.
      3. Si Gemini responde con ``functionCall`` → ejecutar la tool vía
         ``tools.registry.execute_tool`` → enviar ``functionResponse`` →
         segunda llamada → devolver texto final.

    ``tool_context`` contiene datos ocultos para Gemini (ej: ``steam_id``)
    que se inyectan al ejecutar las tools.

    Un solo turno de tool por mensaje (sin cadenas).
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY no configurada")

    # Construir contents desde el historial
    contents: list[dict] = []
    for turn in history:
        text = (turn.get("content") or "").strip()
        if not text:
            continue
        role = "model" if turn.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    # Construir body
    body: dict = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_PROMPT_TOOLS}]},
        "contents": contents,
    }
    if tools:
        body["tools"] = [{"functionDeclarations": tools}]

    url = f"{_GEMINI_BASE}/{GEMINI_MODEL}:generateContent"

    # ── Primera llamada ────────────────────────────────────────────────────
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

    # ¿Gemini devolvió texto directamente?
    text = _extract_text(candidates)
    if text:
        return text

    # ¿Gemini pidió ejecutar una tool?
    fc_part = _extract_function_call(candidates)
    if not fc_part:
        raise RuntimeError("Respuesta vacía de Gemini (sin texto ni functionCall)")

    fc_call = fc_part["functionCall"]
    fn_name = fc_call.get("name", "")
    fn_args = fc_call.get("args", {})
    logger.info("[gemini-tools] functionCall: %s(%s)", fn_name, fn_args)

    # ── Ejecutar la tool ──────────────────────────────────────────────────
    from tools.registry import execute_tool

    try:
        ctx = tool_context or {}
        result = await execute_tool(
            fn_name,
            steam_id=ctx.get("steam_id"),
            client=client,
            **fn_args,
        )
    except (KeyError, ValueError) as exc:
        logger.warning("[gemini-tools] tool '%s' falló: %s", fn_name, exc)
        return f"No pude ejecutar la herramienta '{fn_name}': {exc}"
    except Exception as exc:
        logger.warning("[gemini-tools] error ejecutando '%s': %s", fn_name, exc)
        return f"Error al ejecutar '{fn_name}': {exc}"

    # ── Segunda llamada con functionResponse ───────────────────────────────
    import json

    result_str = json.dumps(result, ensure_ascii=False, default=str)

    # Reenviar el functionCall EXACTAMENTE como Gemini lo devolvió
    # (incluyendo thoughtSignature si existe — requerido por Gemini 3)
    contents.append({"role": "model", "parts": [fc_part]})
    contents.append({
        "role": "user",
        "parts": [{"functionResponse": {"name": fn_name, "response": {"result": result_str}}}],
    })

    resp2 = await client.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json=body,
        timeout=_GEMINI_TIMEOUT,
    )
    resp2.raise_for_status()

    data2 = resp2.json()
    candidates2 = data2.get("candidates", [])
    text2 = _extract_text(candidates2)
    if text2:
        return text2

    raise RuntimeError("Gemini no devolvió texto tras functionResponse")
