"""Tests de la query de retrieval con contexto conversacional (chat/agent.py)."""

from chat.agent import _retrieval_query, _QUERY_HISTORY_TURNS, _QUERY_MAX_CHARS


def test_sin_historial_devuelve_el_mensaje():
    assert _retrieval_query("¿qué trae el parche?", []) == "¿qué trae el parche?"


def test_seguimiento_hereda_el_tema_del_turno_previo():
    history = [{"role": "user", "content": "¿qué trae el último parche de CS2?"}]
    query = _retrieval_query("¿y eso afecta a las AK?", history)
    # El pronombre solo no recuperaría nada: el tema debe venir del turno previo.
    assert "parche" in query
    assert "AK" in query


def test_ignora_respuestas_del_modelo():
    history = [
        {"role": "user", "content": "¿qué trae el parche?"},
        {"role": "assistant", "content": "El parche incluye cambios de mapas " * 20},
    ]
    query = _retrieval_query("¿y las skins?", history)
    assert "cambios de mapas" not in query
    assert "parche" in query


def test_solo_los_ultimos_turnos_de_usuario():
    history = [{"role": "user", "content": f"tema{i}"} for i in range(6)]
    query = _retrieval_query("actual", history)
    # Solo los _QUERY_HISTORY_TURNS últimos entran; los antiguos se descartan.
    assert "tema0" not in query
    assert f"tema{6 - _QUERY_HISTORY_TURNS}" in query
    assert query.endswith("actual")


def test_query_se_recorta_al_maximo():
    history = [{"role": "user", "content": "x" * 5000}]
    assert len(_retrieval_query("y" * 5000, history)) <= _QUERY_MAX_CHARS


def test_turnos_vacios_se_ignoran():
    history = [{"role": "user", "content": "   "}, {"role": "user", "content": "karambit"}]
    query = _retrieval_query("¿precio?", history)
    assert "karambit" in query
