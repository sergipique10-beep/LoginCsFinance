"""Filtro de ruido de granularidad (tools/market_tools.py:_tiene_senal).

Caso real de producción: a "¿qué items recomiendas comprar?" el agente destacó
una Galil AR | Green Apple a $0.10 con "+11.11%" como item en tendencia. Ese
11% es UN centavo — el tick mínimo del mercado. Medido en `market_trending`, el
61% de los items eran de ~$0.11 con volatilidad aparente 3.6x la de los de $2-10.

El modelo no se lo inventaba: describía fielmente lo que la tool le mandaba. El
sesgo estaba en la fuente, así que el filtro va aquí y no en el prompt.
"""

from tools.market_tools import _PRECIO_MIN_LLM, _tiene_senal, _para_llm


def _item(precio: float, delta: float | None = 5.0, nombre: str = "X") -> dict:
    return {"name": nombre, "priceLatest": precio, "priceDelta24h": delta}


class TestRuidoDeCentimos:
    def test_galil_green_apple_queda_fuera(self):
        """El caso literal de producción: $0.10 con +11.11% = un tick."""
        assert _tiene_senal(_item(0.1028, 11.11, "Galil AR | Green Apple (MW)")) is False

    def test_movimiento_enorme_en_item_de_centimos_queda_fuera(self):
        """+42.86% sobre $0.088 son ~4 ticks: sigue sin ser capturable."""
        assert _tiene_senal(_item(0.0879, 42.86, "AK-47 | Safari Mesh (FT)")) is False

    def test_item_barato_no_pasa_ni_con_delta_nulo(self):
        assert _tiene_senal(_item(0.04, None)) is False


class TestSenalReal:
    def test_movimiento_pequeno_en_item_caro_si_pasa(self):
        """En una AK de $28 un tick es 0.04%: un +0.25% es señal de verdad."""
        assert _tiene_senal(_item(28.0, 0.25, "AK-47 | Redline (FT)")) is True

    def test_item_de_6_dolares_ya_no_pasa(self):
        """El suelo subió a ~10 EUR: por debajo no se considera valioso.

        Antes pasaba con suelo $0.50. El criterio ahora no es solo "¿el movimiento
        es real?" sino "¿el item merece atención?".
        """
        assert _tiene_senal(_item(6.05, 0.25, "AK-47 | Ice Coaled (FT)")) is False

    def test_sin_delta_pero_con_precio_pasa(self):
        """Sin dato de movimiento no hay nada que juzgar: decide el modelo."""
        assert _tiene_senal(_item(15.0, None)) is True

    def test_glove_case_pasa(self):
        """Caso real de movers: $20.35 con -3.3% en 7d."""
        assert _tiene_senal(_item(20.35, -3.3, "Glove Case")) is True


class TestUmbrales:
    def test_el_suelo_de_precio_se_aplica(self):
        assert _tiene_senal(_item(_PRECIO_MIN_LLM - 0.01, 50.0)) is False
        assert _tiene_senal(_item(_PRECIO_MIN_LLM + 0.01, 50.0)) is True

    def test_movimiento_por_debajo_de_un_tick_no_pasa(self):
        """Un +0.5% en un item de $1 es medio tick: no se puede ni ejecutar."""
        assert _tiene_senal(_item(1.0, 0.5)) is False

    def test_item_plano_no_pasa(self):
        """delta 0.0 es un dato real (no se movió), no un faltante."""
        assert _tiene_senal(_item(0.92, 0.0)) is False


class TestParaLlm:
    def test_filtra_el_ruido_de_la_lista(self):
        items = [_item(0.10, 11.11, "ruido"), _item(28.0, 3.0, "senal")]
        nombres = [i["name"] for i in _para_llm(items)]
        assert nombres == ["senal"]

    def test_lista_entera_de_ruido_no_devuelve_vacio(self):
        """Vaciar la lista haría creer al modelo que no hay mercado.

        Es peor que devolver los datos crudos: sin nada que decir, rellenaría el
        hueco de memoria. Con los items delante puede explicar que son de céntimos.
        """
        items = [_item(0.10, 11.11, "a"), _item(0.04, 25.0, "b")]
        assert len(_para_llm(items)) == 2
