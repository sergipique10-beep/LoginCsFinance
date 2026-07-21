"""Agente Sharky: chat con function calling sobre Gemini.

Loop multi-turno: el modelo puede pedir varias tools por vuelta (p. ej. noticias
y predicción a la vez) y encadenar varias vueltas hasta responder. El transporte
al LLM vive en llm/; las tools en tools/. `tool_context` lleva datos ocultos a
Gemini (ej. steam_id).

Antes de la primera llamada se hace un retrieval del RAG sobre el mensaje del
usuario y se inyecta en el system prompt, de modo que el contexto de noticias ya
está disponible sin gastar una vuelta de tool.
"""
import asyncio
import json
import logging

import httpx

from llm.gemini import call, extract_text
from chat.prompts import _SYSTEM_PROMPT_TOOLS, with_rag_context

logger = logging.getLogger("uvicorn.error")

# Tope de vueltas de tool. Cada vuelta es una llamada extra a Gemini: sin tope,
# un modelo que insista en pedir tools dejaría la petición colgada.
MAX_TOOL_TURNS = 3

# Veces que se ejecuta la MISMA tool en una petición. Al no encontrar una skin,
# el modelo prueba variantes del nombre ("Pink DDPAT", "Unicorn"...) y cada
# intento se acumula en `contents`: visto en producción llegando a 28 KB y
# tumbando la respuesta con un 503. A partir del tope se le devuelve un error
# explicativo en vez de ejecutar la búsqueda, que es lo que corta el bucle.
MAX_USOS_POR_TOOL = 2

# Turnos previos del usuario que se anteponen a la query de retrieval, para que
# un seguimiento con pronombres ("¿y eso?") conserve el tema.
_QUERY_HISTORY_TURNS = 2
# Tope de la query embebida: más texto diluye el embedding en vez de precisarlo.
_QUERY_MAX_CHARS = 600

# Términos que hacen que un mensaje se considere "sobre noticias". La lista es
# deliberadamente amplia y el sesgo es a recuperar de más: un falso negativo hace
# que el modelo responda de memoria sobre parches (alucina), mientras que un falso
# positivo solo cuesta un embedding. Los errores no son simétricos.
_TEMAS_RAG = (
    "noticia", "novedad", "nuevo", "nueva", "parche", "patch", "update",
    "actualiz", "operacion", "operación", "caso", "case", "colección",
    "coleccion", "temporada", "season", "evento", "torneo", "major",
    "cambio", "cambiar", "nerf", "buff", "mapa", "map pool", "active duty",
    "por que", "por qué", "porque", "anunci", "salió", "salio", "lanz",
    "armory", "reciente",
    # Intención de compra/inversión: "¿qué compro hoy?" no menciona noticias,
    # pero un parche o una colección retirada de la Armería es justo el contexto
    # que separa una recomendación informada de una lista de deltas sueltos.
    "comprar", "compra", "compro", "recomiend", "oportunidad", "invertir",
    "inversión", "inversion", "merece la pena", "vale la pena", "conviene",
    # Ojo con los adjetivos temporales sueltos: "último"/"pasado" casan con
    # "el último mes" o "el mes pasado", que son preguntas de precio, no de
    # noticias. Van solo acompañados del sustantivo que sí fija el tema.
    "últimas noticias", "ultimas noticias", "últimas novedades", "ultimas novedades",
)

# Pronombres/deícticos sin referente propio: un seguimiento como "¿y eso?" no
# contiene el tema, pero lo hereda del turno anterior. Si el mensaje es corto y
# lleva uno de estos, se recupera igual — es justo el caso que más alucina.
_DEICTICOS = ("eso", "esto", "ello", "aquello", "lo mismo", "y qué", "y que")
_MSG_CORTO_CHARS = 60


def necesita_rag(message: str, history: list[dict] | None = None) -> bool:
    """Decide si el turno merece retrieval, sin llamar a nadie.

    Alternativa a preguntárselo al modelo (una vuelta extra de LLM) o a recuperar
    siempre (un embedding por turno). Ante la duda, devuelve True.
    """
    txt = (message or "").lower()
    if any(t in txt for t in _TEMAS_RAG):
        return True
    # Seguimiento corto con deíctico: el tema está en el turno previo, no aquí.
    if len(txt) <= _MSG_CORTO_CHARS and any(d in txt for d in _DEICTICOS):
        previos = [
            (t.get("content") or "").lower()
            for t in (history or [])
            if t.get("role") != "assistant"
        ]
        ctx = " ".join(previos[-_QUERY_HISTORY_TURNS:])
        return any(t in ctx for t in _TEMAS_RAG)
    return False


def _extract_function_calls(candidates: list[dict]) -> list[dict]:
    """Todas las parts con functionCall de la primera candidate (no solo la primera).

    Gemini puede devolver varias functionCall en una misma respuesta; ejecutarlas
    todas es lo que permite combinar, p. ej., noticias + predicción en un turno.
    """
    if not candidates:
        return []
    parts = candidates[0].get("content", {}).get("parts", []) or []
    return [p for p in parts if "functionCall" in p]


class ToolUnavailable(Exception):
    """La tool no existe o falta contexto obligatorio: no hay nada que reintentar.

    Se corta ahí y se responde al usuario, sin gastar otra llamada a Gemini —
    a diferencia de un fallo de ejecución (red, API caída), que sí se le
    devuelve al modelo para que lo explique con sus palabras.
    """


async def _run_tool(fc_part: dict, client: httpx.AsyncClient, ctx: dict) -> dict:
    """Ejecuta una tool y devuelve su part de functionResponse.

    Lanza ToolUnavailable si la tool no está registrada o falta steam_id.
    """
    from tools.registry import execute_tool

    fc_call = fc_part["functionCall"]
    fn_name = fc_call.get("name", "")
    fn_args = fc_call.get("args", {}) or {}
    logger.info("[chat-agent] functionCall: %s(%s)", fn_name, fn_args)

    usos = ctx.setdefault("_usos_tool", {})
    usos[fn_name] = usos.get(fn_name, 0) + 1
    if usos[fn_name] > MAX_USOS_POR_TOOL:
        logger.info(
            "[chat-agent] '%s' agotó sus %d usos: se corta el bucle de reintentos",
            fn_name, MAX_USOS_POR_TOOL,
        )
        return {
            "functionResponse": {
                "name": fn_name,
                "response": {"result": json.dumps({
                    "error": (
                        f"Ya has usado '{fn_name}' {MAX_USOS_POR_TOOL} veces en esta "
                        "consulta sin resultado útil. No pruebes más variantes: "
                        "responde con lo que tengas o di que no lo encuentras."
                    )
                }, ensure_ascii=False)},
            }
        }

    # El sink solo lo acepta la tool de RAG; pasarlo a las demás sería un
    # argumento inesperado.
    if fn_name == "buscar_contexto_rag" and ctx.get("sources_sink") is not None:
        fn_args["_sources_sink"] = ctx["sources_sink"]

    try:
        result = await execute_tool(
            fn_name, steam_id=ctx.get("steam_id"), client=client, **fn_args
        )
    except (KeyError, ValueError) as exc:
        logger.warning("[chat-agent] tool '%s' no disponible: %s", fn_name, exc)
        raise ToolUnavailable(f"No pude ejecutar la herramienta '{fn_name}': {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — fallo de ejecución: que lo explique el modelo
        logger.warning("[chat-agent] error ejecutando '%s': %s", fn_name, exc)
        result = {"error": f"error al ejecutar '{fn_name}': {exc}"}

    return {
        "functionResponse": {
            "name": fn_name,
            "response": {"result": json.dumps(result, ensure_ascii=False, default=str)},
        }
    }


def _retrieval_query(message: str, history: list[dict]) -> str:
    """Query de retrieval: mensaje actual + los últimos turnos del usuario.

    Un seguimiento como "¿y eso afecta a las AK?" embebido solo no recupera nada
    útil: los pronombres no llevan el tema. Anteponer los turnos previos del
    usuario devuelve ese tema a la query. Solo turnos de usuario — las respuestas
    del modelo son largas y diluirían el embedding.
    """
    previos = [
        (t.get("content") or "").strip()
        for t in history
        if t.get("role") != "assistant" and (t.get("content") or "").strip()
    ]
    partes = previos[-_QUERY_HISTORY_TURNS:] + [message]
    return " ".join(partes)[:_QUERY_MAX_CHARS]


async def _retrieve_context(
    client: httpx.AsyncClient, message: str, history: list[dict] | None = None
) -> list[dict]:
    """Chunks del RAG para el turno actual. Best-effort: si falla, sin contexto.

    Solo se embebe si `necesita_rag` dice que el turno va de noticias: una
    pregunta de precio no gasta embedding. Con CHAT_RAG_PRELOAD=false se
    desactiva del todo y el modelo depende de la tool `buscar_contexto_rag`.
    """
    from settings import CHAT_RAG_PRELOAD
    if not CHAT_RAG_PRELOAD:
        return []
    if not necesita_rag(message, history):
        logger.info("[chat-agent] retrieval omitido: el turno no parece de noticias")
        return []
    query = _retrieval_query(message, history or [])
    try:
        from rag.retrieval import retrieve
        return await retrieve(client, query, k=3)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[chat-agent] retrieval falló: %s", exc)
        return []


async def generate_with_sources(
    client: httpx.AsyncClient,
    message: str,
    history: list[dict],
    tools: list[dict] | None = None,
    tool_context: dict | None = None,
) -> tuple[str, list[dict]]:
    """Igual que `generate_with_tools`, pero devuelve `(texto, fragmentos_rag)`.

    Los fragmentos son los de ESTE mensaje por cualquiera de las dos vías: el
    preload del system prompt y la tool `buscar_contexto_rag`. Ambas cuentan —
    si solo se miraran los del preload, una respuesta documentada vía tool
    llegaría al frontend sin fuentes. El router deduplica por URL.
    """
    fragmentos = await _retrieve_context(client, message, history)
    sink: list[dict] = []
    ctx = {**(tool_context or {}), "sources_sink": sink}
    texto = await _generate(client, message, history, tools, ctx, fragmentos)
    return texto, fragmentos + sink


async def generate_with_tools(
    client: httpx.AsyncClient,
    message: str,
    history: list[dict],
    tools: list[dict] | None = None,
    tool_context: dict | None = None,
) -> str:
    """Genera una respuesta con soporte de function calling multi-turno."""
    fragmentos = await _retrieve_context(client, message, history)
    return await _generate(client, message, history, tools, tool_context, fragmentos)


async def _generate(
    client: httpx.AsyncClient,
    message: str,
    history: list[dict],
    tools: list[dict] | None,
    tool_context: dict | None,
    fragmentos: list[dict],
) -> str:
    """Loop de generación. El retrieval ya viene hecho por el caller."""
    contents: list[dict] = []
    for turn in history:
        text = (turn.get("content") or "").strip()
        if not text:
            continue
        role = "model" if turn.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    system_prompt = with_rag_context(_SYSTEM_PROMPT_TOOLS, fragmentos)

    body: dict = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
    }
    if tools:
        body["tools"] = [{"functionDeclarations": tools}]

    # Copia propia: `_run_tool` acumula aquí el contador de usos por tool, y el
    # dict del caller puede sobrevivir a la petición (el contador se filtraría
    # a la siguiente y cortaría búsquedas legítimas).
    ctx = dict(tool_context or {})

    for _ in range(MAX_TOOL_TURNS):
        data = await call(client, body)
        candidates = data.get("candidates", [])
        if not candidates:
            feedback = data.get("promptFeedback", {})
            logger.warning("Gemini sin candidates. promptFeedback=%s", feedback)
            raise RuntimeError("Gemini no devolvió respuesta (posible bloqueo de seguridad)")

        fc_parts = _extract_function_calls(candidates)
        if not fc_parts:
            text = extract_text(candidates)
            if text:
                return text
            raise RuntimeError("Respuesta vacía de Gemini (sin texto ni functionCall)")

        # Todas las tools de esta vuelta, en paralelo: son independientes entre sí.
        try:
            responses = await asyncio.gather(
                *(_run_tool(p, client, ctx) for p in fc_parts)
            )
        except ToolUnavailable as exc:
            return str(exc)  # nada que reintentar: se responde sin otra llamada
        contents.append({"role": "model", "parts": fc_parts})
        contents.append({"role": "user", "parts": list(responses)})

    # Agotadas las vueltas: última llamada sin tools para forzar una respuesta.
    body.pop("tools", None)
    data = await call(client, body)
    text = extract_text(data.get("candidates", []))
    if text:
        return text
    raise RuntimeError("Respuesta vacía de Gemini tras las tools")
