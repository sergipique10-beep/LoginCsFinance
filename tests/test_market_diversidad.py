"""Diversificación de los rankings (steam/routes/market.py:_diversificar).

`_category_rank` ordenaba por prioridad de categoría, lo que AGOTA la primera
antes de pasar a la siguiente: con "Rifle" en cabeza los 18 huecos del trending
salían todos rifles, incluidas 4 variantes de desgaste de la misma skin.
"""

from steam.routes.market import (
    _MAX_POR_CATEGORIA, _MAX_POR_SKIN, _diversificar, _skin_base,
)


def _item(nombre: str, categoria: str) -> dict:
    return {"name": nombre, "weaponType": categoria}


class TestSkinBase:
    def test_ignora_el_desgaste(self):
        a = _skin_base("AK-47 | Crane Flight (Field-Tested)")
        b = _skin_base("AK-47 | Crane Flight (Minimal Wear)")
        assert a == b

    def test_distingue_skins_distintas(self):
        assert _skin_base("AK-47 | Redline (FT)") != _skin_base("AK-47 | Slate (FT)")

    def test_sin_desgaste_no_rompe(self):
        assert _skin_base("Glove Case") == "glove case"


class TestCuotaPorCategoria:
    def test_no_deja_que_una_categoria_copie_la_lista(self):
        items = [_item(f"AK-47 | Skin{i} (FT)", "Rifle") for i in range(20)]
        items += [_item(f"AWP | Skin{i} (FT)", "Sniper Rifle") for i in range(20)]
        items += [_item(f"Glock-18 | Skin{i} (FT)", "Pistol") for i in range(20)]

        r = _diversificar(items, 9)
        cats = [i["weaponType"] for i in r]
        assert cats.count("Rifle") <= _MAX_POR_CATEGORIA
        assert len(set(cats)) == 3

    def test_respeta_el_orden_de_relevancia_dentro_de_la_cuota(self):
        """Los primeros de cada categoría son los que venían antes en la lista."""
        items = [_item(f"AK-47 | Skin{i} (FT)", "Rifle") for i in range(10)]
        r = _diversificar(items, 4)
        assert [i["name"] for i in r][:_MAX_POR_CATEGORIA] == [
            f"AK-47 | Skin{i} (FT)" for i in range(_MAX_POR_CATEGORIA)
        ]


class TestCuotaPorSkin:
    def test_limita_variantes_de_desgaste(self):
        """Crane Flight FT/MW/WW/BS es el mismo activo repetido 4 veces."""
        items = [
            _item("AK-47 | Crane Flight (Field-Tested)", "Rifle"),
            _item("AK-47 | Crane Flight (Minimal Wear)", "Rifle"),
            _item("AK-47 | Crane Flight (Well-Worn)", "Rifle"),
            _item("AK-47 | Crane Flight (Battle-Scarred)", "Rifle"),
            _item("AWP | Asiimov (Field-Tested)", "Sniper Rifle"),
        ]
        r = _diversificar(items, 5)
        cranes = [i for i in r if "Crane Flight" in i["name"]]
        assert len(cranes) <= _MAX_POR_SKIN


class TestRelleno:
    def test_no_devuelve_lista_corta_si_falta_variedad(self):
        """Sin variedad suficiente, es peor una lista a medias que una repetida."""
        items = [_item(f"AK-47 | Skin{i} (FT)", "Rifle") for i in range(10)]
        assert len(_diversificar(items, 8)) == 8

    def test_no_inventa_items_si_no_los_hay(self):
        items = [_item("AK-47 | Redline (FT)", "Rifle")]
        assert len(_diversificar(items, 10)) == 1

    def test_lista_vacia(self):
        assert _diversificar([], 5) == []
