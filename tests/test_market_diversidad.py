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


class TestCuotasEscalanConElLimite:
    """Las cuotas se calibraron para 18 huecos; a 500 descartarían de más.

    `_MAX_POR_SKIN` descarta PERMANENTE (no va a relleno), así que con 5
    desgastes por skin un tope fijo de 2 tiraba el 60% de las variantes — items
    que a 500 huecos caben de sobra.
    """

    def test_a_limites_pequenos_no_cambia_nada(self):
        """18 (fallback trending) y 20 (movers) deben dar las cuotas de siempre.

        Con material de sobra en cada categoría, las cuotas gobiernan la lista
        entera (no hace falta tirar de `relleno`), así que el reparto tiene que
        ser exactamente el de antes: _MAX_POR_CATEGORIA por categoría.
        """
        cats_disponibles = ["Rifle", "Sniper Rifle", "Pistol", "SMG", "Knife"]
        items = [
            _item(f"{c} | Skin{i} (FT)", c)
            for c in cats_disponibles for i in range(20)
        ]

        for limite in (18, 20):
            cats = [i["weaponType"] for i in _diversificar(items, limite)]
            for c in cats_disponibles:
                assert cats.count(c) <= _MAX_POR_CATEGORIA, (limite, c)

    def test_a_500_no_tira_las_variantes_de_desgaste(self):
        desgastes = ["Field-Tested", "Minimal Wear", "Well-Worn",
                     "Battle-Scarred", "Factory New"]
        items = [
            _item(f"AK-47 | Skin{i} ({w})", "Rifle")
            for i in range(80) for w in desgastes
        ]
        r = _diversificar(items, 500)
        # Con _MAX_POR_SKIN fijo a 2 saldrían ~160 de los 400.
        assert len(r) == 400

    def test_a_500_sigue_repartiendo_la_cabecera(self):
        """Escalar las cuotas no puede degenerar en "no diversificar nada"."""
        items = [_item(f"AK-47 | Skin{i} (FT)", "Rifle") for i in range(400)]
        items += [_item(f"AWP | Skin{i} (FT)", "Sniper Rifle") for i in range(400)]
        cabecera = [i["weaponType"] for i in _diversificar(items, 500)[:100]]
        assert len(set(cabecera)) == 2

    def test_lista_vacia_con_limite_grande(self):
        assert _diversificar([], 500) == []


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
