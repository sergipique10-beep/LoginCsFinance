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
