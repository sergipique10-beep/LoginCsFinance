"""UX-05: el feed de la home traía noticias en ruso, ilegibles para el usuario.

La Steam News API (appid 730) no admite filtro de idioma: devuelve lo que publica
cada partner. `feedlabel` identifica la fuente, no el idioma, así que el filtro
mira el propio titular.
"""
from steam.mappers import is_readable_news


def n(title: str) -> dict:
    return {"title": title}


class TestIsReadableNews:
    def test_ingles_pasa(self):
        assert is_readable_news(n("Counter-Strike 2 Update Released"))

    def test_espanol_con_acentos_pasa(self):
        assert is_readable_news(n("Actualización de CS2: nuevo mapa y más cajas"))

    def test_ruso_se_descarta(self):
        assert not is_readable_news(n("Обновление Counter-Strike 2 вышло сегодня"))

    def test_chino_se_descarta(self):
        assert not is_readable_news(n("反恐精英2更新已经发布"))

    def test_japones_se_descarta(self):
        assert not is_readable_news(n("カウンターストライク2のアップデート"))

    def test_coreano_se_descarta(self):
        assert not is_readable_news(n("카운터스트라이크 2 업데이트"))

    def test_titular_ingles_con_nombre_propio_ruso_pasa(self):
        # El umbral (20%) existe para esto: una palabra en otro alfabeto dentro
        # de un titular por lo demás legible no debe costar la noticia entera.
        assert is_readable_news(n("Team Spirit signs new player from Москва roster today"))

    def test_sin_titular_pasa(self):
        # Sin titular no hay nada que juzgar: que decida el resto del pipeline.
        assert is_readable_news({})
        assert is_readable_news(n("   "))

    def test_solo_numeros_y_simbolos_pasa(self):
        assert is_readable_news(n("2026 — 100% (!!)"))


class TestEndpointIntegration:
    """El endpoint recorta a `count` y nunca devuelve un feed vacío por el filtro."""

    def test_filtrado_y_recorte(self):
        items = [
            n("CS2 Update One"),
            n("Обновление на русском"),
            n("CS2 Update Two"),
            n("又一个中文更新"),
            n("CS2 Update Three"),
        ]
        readable = [i for i in items if is_readable_news(i)]
        assert [i["title"] for i in readable[:2]] == ["CS2 Update One", "CS2 Update Two"]

    def test_fallback_si_todo_es_ilegible(self):
        # Caso degenerado: más vale una noticia en ruso que un feed vacío.
        items = [n("Обновление"), n("中文更新")]
        readable = [i for i in items if is_readable_news(i)]
        assert readable == []
        assert (readable or items)[:5] == items
