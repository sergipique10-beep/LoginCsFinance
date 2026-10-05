"""Reglas de negocio de steam/ (CLEAN-17): las decisiones sobre un ítem, un ranking, una
tasa o una noticia que antes vivían como strings y comparaciones sueltas en services y
mappers. Puras: reciben el modelo interno (o la tarjeta ya mapeada, en los rankings) y
devuelven un veredicto. Ninguna es nueva; endurecerlas cambia lo que ve el usuario (UX-46).

- Plausibilidad (CLEAN-12): `plausible_ratio`, `plausible_fx_rate`.
- Rankings: `ranking_eligible`, `turnover`, `diversificar`.
- Slabs: `is_sticker_slab(item)` decide por `item_type` primero y por el nombre después.
- Noticias (UX-05): `is_readable_news`, `news_category`.
"""
import re
from collections.abc import Mapping
from typing import Any, TypeVar

from typing_extensions import TypeIs

from steam.domain.enums import NewsCategory
from steam.domain.models import NewsEntry, SteamItem
from steam.domain.normalizers import has_slab_mark, skin_base

# ── Plausibilidad ─────────────────────────────────────────────────────────────

# Un precio histórico fuera de este rango respecto al actual es basura de la API
# (visto: pricereal30d=0.22 para una skin de 17.57 → +7886%), no un movimiento real.
MAX_PLAUSIBLE_RATIO = 10.0

# Un tipo USD/EUR fuera de este rango es un error de la fuente, no un movimiento de
# mercado: mejor servir el último bueno que corromper precios (UX-08).
FX_MIN, FX_MAX = 0.5, 2.0


def plausible_ratio(new: float, old: float) -> bool:
    """¿Está el precio viejo a menos de MAX_PLAUSIBLE_RATIO veces del nuevo? (ambos > 0)"""
    return 1 / MAX_PLAUSIBLE_RATIO <= old / new <= MAX_PLAUSIBLE_RATIO


def plausible_fx_rate(rate: Any) -> TypeIs[float]:
    return isinstance(rate, (int, float)) and FX_MIN < rate < FX_MAX


# ── Rankings ──────────────────────────────────────────────────────────────────

# Suelo de precio para entrar en los rankings (USD; ~10 EUR a 1.08 USD/EUR).
# Por debajo, el movimiento porcentual es ruido de granularidad: los precios de
# Steam se mueven de centavo en centavo, así que en un item de $0.10 un solo tick
# ya es un +10% que ni el spread ni las comisiones (~15%) dejan capturar. Medido
# en producción: el 61% del trending eran items de ~$0.11 con volatilidad
# aparente 3.6x la de los de $2-10, y el agente los presentaba como "en auge".
MIN_RANKING_PRICE = 10.80
# Ventas en 24 h para entrar: los movers piden más que el trending.
MIN_SOLD_MOVERS = 5
MIN_SOLD_TRENDING = 1

# Cuántos items como mucho de una misma categoría en el ranking. Ordenar por
# prioridad de categoría (el antiguo `_category_rank`) AGOTA la primera antes de pasar a la
# siguiente: con "Rifle" en cabeza, los 18 huecos salían todos rifles (4 variantes
# de la misma skin incluidas). El material para diversificar existe — con
# max=5000 hay 113 rifles, 47 pistolas, 41 snipers, 18 SMG, 6 cuchillos — solo
# había que repartir en vez de ordenar.
MAX_POR_CATEGORIA = 4

# Variantes de desgaste de una misma skin (Crane Flight FT/MW/WW/BS) son el mismo
# activo a efectos de "qué está pasando en el mercado". Tope aparte del de
# categoría, que no las distingue.
MAX_POR_SKIN = 2

_Card = TypeVar("_Card", bound=Mapping[str, Any])


def ranking_eligible(item: SteamItem, min_sold: int) -> bool:
    """Precio y ventas mínimos de un item de /items para entrar en un ranking."""
    return (item.price_latest_sell or 0) >= MIN_RANKING_PRICE and (item.sold_24h or 0) >= min_sold


def canonical_price(item: SteamItem | None) -> float | None:
    """Precio canónico de un item de /item: pricelatestsell → pricelatest → pricemedian
    (el primero > 0). None si no hay item o ningún precio."""
    if item is None:
        return None
    for value in (item.price_latest_sell, item.price_latest, item.price_median):
        if value is not None and value > 0:
            return value
    return None


def turnover(item: Mapping[str, Any]) -> float:
    """Facturación 24h estimada de una tarjeta: precio × unidades vendidas.

    Criterio de relevancia para los rankings, en vez de las unidades sueltas.
    Ordenar por unidades premia lo barato por construcción — una Galil de $0.10
    vende más piezas que una AK de $28 aunque mueva 17x menos dinero.
    """
    return (item.get("priceLatest") or 0) * (item.get("sold24h") or 0)


def diversificar(items: list[_Card], limite: int) -> list[_Card]:
    """Reparte el ranking entre categorías en vez de agotarlas por prioridad.

    Recorre los candidatos ya ordenados por relevancia y va aceptando mientras la
    categoría (y la skin base) no hayan llenado su cuota. Si al final sobran
    huecos —porque no hay bastante variedad— se rellenan con los descartados en
    orden, para no devolver una lista más corta de lo pedido.
    """
    aceptados: list[_Card] = []
    # Solo se reservan para relleno los descartados por cuota de CATEGORÍA: una
    # lista corta es peor que una con dos rifles de más. Las variantes de desgaste
    # de una misma skin no vuelven nunca — cuatro Crane Flight no dicen nada que
    # no diga una, y ocupan el hueco de un activo distinto.
    relleno: list[_Card] = []
    por_categoria: dict[str, int] = {}
    por_skin: dict[str, int] = {}

    # Las cuotas se calibraron para 18 huecos, donde su trabajo era evitar que
    # "Rifle" agotara la lista entera. A 500 esas mismas cifras descartarían
    # items que sí caben: con MAX_POR_SKIN=2 y 5 desgastes por skin se tiraba
    # el 60% de las variantes. Escalan con el límite porque la diversificación
    # solo tiene que proteger la CABECERA, que es lo que se ve sin scroll.
    # A limite=18 (fallback) y limite=20 (movers) dan exactamente (4, 2) — el
    # comportamiento de hoy, sin regresión.
    max_categoria = max(MAX_POR_CATEGORIA, limite // 8)
    max_skin = max(MAX_POR_SKIN, limite // 100)

    for it in items:
        cat = it.get("weaponType") or "?"
        base = skin_base(it.get("name") or "")
        if por_skin.get(base, 0) >= max_skin:
            continue
        if por_categoria.get(cat, 0) >= max_categoria:
            relleno.append(it)
            continue
        por_categoria[cat] = por_categoria.get(cat, 0) + 1
        por_skin[base] = por_skin.get(base, 0) + 1
        aceptados.append(it)
        if len(aceptados) >= limite:
            return aceptados

    return (aceptados + relleno)[:limite]


# ── Slabs ─────────────────────────────────────────────────────────────────────

def is_sticker_slab(item: SteamItem) -> bool:
    """Un sticker slab no entra en rankings ni búsquedas (no es una skin).

    Decide por `item_type` primero («Sticker Slab» en steamwebapi, fixture
    `tests/fixtures/steamwebapi/items.json`). Si el itemtype no lo dice —falta, o
    la API lo clasifica como «sticker» a secas, que es lo que fija el contrato de
    los ticks— decide el nombre, canónico o localizado, igual que antes de CLEAN-17.
    """
    if has_slab_mark(item.item_type):
        return True
    return has_slab_mark(item.market_name) or has_slab_mark(item.market_hash_name)


# ── Noticias ──────────────────────────────────────────────────────────────────

# UX-05: la Steam News API (appid 730) no admite filtro de idioma — devuelve lo
# que publica cada partner, y los medios rusos y chinos publican en su idioma.
# `feedlabel` identifica la fuente, no el idioma, así que no sirve para filtrar.
# Se mira el texto: si una fracción apreciable del titular es cirílico o CJK, la
# noticia es ilegible para el usuario objetivo y se descarta.
_NON_LATIN_RE = re.compile(
    r"[Ѐ-ӿ"      # cirílico
    r"一-鿿"       # han (chino / kanji)
    r"぀-ヿ"       # kana japonés
    r"가-힯]"      # hangul coreano
)

# Fracción de caracteres no latinos por encima de la cual se descarta. 0.2 deja
# pasar un titular en inglés con una palabra o un nombre propio en otro alfabeto,
# y descarta el que está escrito entero en él.
_NON_LATIN_THRESHOLD = 0.2


def is_readable_news(entry: NewsEntry) -> bool:
    """False si el titular está mayoritariamente en un alfabeto no latino.

    Se mira solo el titular: es lo que el usuario lee en la lista, y el cuerpo
    puede traer markup y nombres propios que ensucian la proporción.
    """
    title = (entry.title or "").strip()
    if not title:
        return True  # sin titular no hay nada que juzgar; que decida el resto

    letters = [c for c in title if c.isalpha()]
    if not letters:
        return True  # solo números o símbolos: no es un idioma

    non_latin = sum(1 for c in letters if _NON_LATIN_RE.match(c))
    return (non_latin / len(letters)) <= _NON_LATIN_THRESHOLD


# Qué substring del `feedname` (en minúsculas) identifica cada fuente, en orden de
# comprobación. Es la tabla de los cinco substrings que `_map_news_item` miraba para
# elegir el color del chip; la salida es la misma.
NEWS_CATEGORY_MARKS: tuple[tuple[str, NewsCategory], ...] = (
    ("blog", NewsCategory.BLOG),
    ("valve", NewsCategory.VALVE),
    ("hltv", NewsCategory.HLTV),
    ("liquipedia", NewsCategory.LIQUIPEDIA),
    ("esport", NewsCategory.ESPORTS),
)


def news_category(feedname: str | None, feedlabel: str | None = None) -> NewsCategory:
    """Fuente de una noticia por su `feedname`; `feedlabel` solo se mira si el feedname
    no viene (los feeds de Steam traen siempre los dos). `OTHER` si ninguna marca encaja."""
    key = (feedname or feedlabel or "").lower()
    for mark, category in NEWS_CATEGORY_MARKS:
        if mark in key:
            return category
    return NewsCategory.OTHER
