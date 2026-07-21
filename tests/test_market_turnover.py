"""Criterio de relevancia de los rankings (steam/routes/market.py).

Ordenar por unidades vendidas premia lo barato por construcción: una Galil de
$0.10 vende más piezas que una AK de $28 aunque mueva 17x menos dinero. El
resultado medido en producción: 0 items por encima de $10 en trending, y el 61%
por debajo de $0.50.
"""

from steam.routes.market import _PRECIO_MIN_RANKING, _turnover


def _item(precio: float, vendidas: int) -> dict:
    return {"priceLatest": precio, "sold24h": vendidas}


class TestTurnover:
    def test_el_caro_gana_aunque_venda_menos_piezas(self):
        galil = _item(0.10, 500)   # $50 de facturación
        ak = _item(28.0, 30)       # $840
        assert _turnover(ak) > _turnover(galil)

    def test_ordena_por_dinero_no_por_piezas(self):
        items = [_item(0.10, 500), _item(28.0, 30), _item(6.0, 50)]
        ordenado = sorted(items, key=_turnover, reverse=True)
        assert [i["priceLatest"] for i in ordenado] == [28.0, 6.0, 0.10]

    def test_tolera_campos_ausentes(self):
        """_map_item colapsa None a 0, pero los snapshots viejos pueden no traerlo."""
        assert _turnover({}) == 0
        assert _turnover({"priceLatest": None, "sold24h": None}) == 0

    def test_volumen_cero_no_puntua(self):
        """Un item caro que no se vende no mueve dinero: no es relevante."""
        assert _turnover(_item(500.0, 0)) == 0


class TestSueloDePrecio:
    def test_el_suelo_ronda_los_10_euros(self):
        # ~1.08 USD/EUR. Si alguien lo cambia, que sea deliberado.
        assert 10.0 <= _PRECIO_MIN_RANKING <= 12.0
