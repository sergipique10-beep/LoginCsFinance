"""Tests del system prompt del chat (chat/prompts.py)."""

from chat.prompts import _SYSTEM_PROMPT_TOOLS, _MAX_CHUNK_CHARS, with_rag_context

_FRAGMENTOS = [
    {
        "title": "Counter-Strike 2 Update",
        "url": "https://example.com/news/1836506165581827",
        "content": "Cache vuelve al Active Duty Map Pool.",
    }
]


def test_sin_fragmentos_devuelve_el_prompt_intacto():
    assert with_rag_context(_SYSTEM_PROMPT_TOOLS, []) == _SYSTEM_PROMPT_TOOLS


def test_el_contexto_inyecta_titulo_y_contenido():
    prompt = with_rag_context(_SYSTEM_PROMPT_TOOLS, _FRAGMENTOS)
    assert "Counter-Strike 2 Update" in prompt
    assert "Active Duty Map Pool" in prompt


def test_no_pide_citar_urls():
    """El modelo copia las URLs de memoria y cambia dígitos → enlace roto.

    Verificado en E2E: citó '...82827' cuando la real era '...81827'. Los enlaces
    viajan en `sources[]`, que sí es exacto; el prompt debe mandar citar por título.
    """
    prompt = with_rag_context(_SYSTEM_PROMPT_TOOLS, _FRAGMENTOS)
    assert "citando el título (nunca la URL)" in prompt
    assert "citando el título o la URL" not in prompt


def test_no_inyecta_urls_en_el_contexto():
    """Si no debe citarlas, no hace falta dárselas: es peso y es tentación."""
    prompt = with_rag_context(_SYSTEM_PROMPT_TOOLS, _FRAGMENTOS)
    assert "https://example.com/news/1836506165581827" not in prompt


def test_recorta_chunks_largos():
    """El bloque de contexto se reenvía en cada vuelta del loop de tools.

    Sin tope, 3 chunks enteros (~4.7 KB medidos) se pagan por vuelta y acercan
    el cuerpo al tamaño donde Gemini devuelve 503.
    """
    largo = [{"title": "T", "url": "u", "content": "x" * 5000}]
    prompt = with_rag_context(_SYSTEM_PROMPT_TOOLS, largo)
    assert "x" * (_MAX_CHUNK_CHARS + 1) not in prompt
    assert "…" in prompt


def test_prohibe_escribir_urls_en_la_respuesta():
    assert "No escribas URLs" in _SYSTEM_PROMPT_TOOLS


def test_acota_el_uso_de_tools_a_datos_vivos():
    """Cada tool es una vuelta extra del loop = otra llamada a Gemini.

    Medido en producción: 'hola' (0 tools, 1 llamada) tarda 2.5-4.6 s, mientras
    que una pregunta de precio (1 tool, 2 llamadas) se va a 21 s. Los casos de
    charla y de concepto general deben responderse de memoria.
    """
    assert "CUÁNDO USAR HERRAMIENTAS:" in _SYSTEM_PROMPT_TOOLS
    assert "quién eres" in _SYSTEM_PROMPT_TOOLS
    assert "qué es el float" in _SYSTEM_PROMPT_TOOLS


def test_pide_respuestas_cortas_por_defecto():
    """Salida más corta = menos tokens generados = menos latencia por vuelta."""
    assert "2-4 frases" in _SYSTEM_PROMPT_TOOLS


def test_sigue_permitiendo_tools_para_datos_de_mercado():
    """Contrapeso del recorte: un precio o una predicción SÍ exigen la tool.

    Sin esto, acotar el uso de tools se convierte en un modelo que responde
    precios de memoria — exactamente lo que el resto del prompt prohíbe.
    """
    assert "un precio, un movimiento, el inventario, una predicción" in _SYSTEM_PROMPT_TOOLS
