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
