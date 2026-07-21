"""Tests del router de retrieval (chat/agent.py:necesita_rag).

Decide si un turno merece gastar un embedding. El sesgo es deliberado: ante la
duda, recuperar. Un falso negativo hace que el modelo responda de memoria sobre
parches (alucina); un falso positivo solo cuesta un embedding.
"""

import pytest

from chat.agent import necesita_rag


@pytest.mark.parametrize("mensaje", [
    "¿Qué novedades hay en CS2?",
    "¿Cuándo sale el próximo parche?",
    "¿Qué trae la actualización de esta semana?",
    "¿Por qué ha subido la AK Redline?",
    "¿Qué mapas hay en el active duty?",
    "Cuéntame de la temporada 5",
    "¿Han nerfeado algo?",
    "¿Qué casos nuevos han sacado?",
])
def test_temas_de_noticias_disparan_retrieval(mensaje):
    assert necesita_rag(mensaje, []) is True


@pytest.mark.parametrize("mensaje", [
    "¿Cuánto vale la AK-47 Redline Field-Tested?",
    "Enséñame mi inventario",
    "¿Cuál es el precio de la AWP Asiimov?",
    "¿Qué skins tengo?",
])
def test_preguntas_de_precio_no_gastan_embedding(mensaje):
    assert necesita_rag(mensaje, []) is False


@pytest.mark.parametrize("mensaje", [
    "¿Cómo se ha comportado el precio de la AWP Asiimov el último mes?",
    "¿Cuál fue el precio máximo el mes pasado?",
    "Dame el último precio de la AK Redline",
])
def test_adjetivos_temporales_no_disparan_retrieval(mensaje):
    """"último"/"pasado" sueltos son de rango temporal, no de noticias.

    Regresión: "el último mes" hacía recuperar en una pregunta de histórico puro
    (caso 3 del E2E), gastando un embedding para nada.
    """
    assert necesita_rag(mensaje, []) is False


def test_seguimiento_con_deictico_hereda_el_tema():
    """"¿Y eso afecta a las AK?" no contiene el tema: lo lleva el turno previo."""
    history = [{"role": "user", "content": "¿Cuáles son las últimas novedades de CS2?"}]
    assert necesita_rag("¿Y eso afecta a las AK?", history) is True


def test_deictico_sin_contexto_de_noticias_no_dispara():
    history = [{"role": "user", "content": "¿cuánto vale mi karambit?"}]
    assert necesita_rag("¿y eso?", history) is False


def test_respuestas_del_modelo_no_cuentan_como_contexto():
    """Solo los turnos del usuario fijan el tema; si no, el modelo se autoconfirma."""
    history = [{"role": "assistant", "content": "El último parche trae mapas nuevos."}]
    assert necesita_rag("¿y eso?", history) is False


def test_mensaje_largo_con_deictico_no_hereda():
    """Un mensaje largo trae su propio tema: si no lo menciona, no va de noticias."""
    history = [{"role": "user", "content": "¿qué novedades hay?"}]
    largo = "eso que me contaste " + "sobre el precio de mi inventario " * 3
    assert necesita_rag(largo, history) is False


def test_entrada_vacia_no_rompe():
    assert necesita_rag("", []) is False
    assert necesita_rag("", None) is False
