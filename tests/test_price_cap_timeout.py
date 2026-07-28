"""El lote del price-tick debe caber en lo que Render free aguanta.

Cada lookup pasa por `_history_limiter` (18 req/60 s), así que el tiempo de un
lote lo fija el número de skins, no la red.

La restricción real NO es el `--max-time` del curl sino **Render free**, que
corta las peticiones HTTP largas por su cuenta. Medido en producción:

    200 skins ≈ 11 min → OK
    400 skins ≈ 22 min → 502, y se perdió la corrida entera (el upsert va al
                         final: 0 puntos escritos ese día)

Por eso el workflow trocea: llama al tick en bucle con lotes de
PRICE_LOOKUP_CAP hasta que `pendientes` llega a 0. Este test protege el tamaño
del lote — si crece hasta volver a rozar el corte de Render, vuelve el 502 y con
él la pérdida de un día entero de serie.
"""

from settings import PRICE_LOOKUP_CAP
from steam.services import _history_limiter

# Cota superior observada del corte de Render free. No es un valor documentado:
# sale de que 11 min pasaban y 22 no. Se toma el extremo conservador.
_RENDER_CORTE_S = 11 * 60
# El lote debe quedar holgadamente por debajo: cada skin suma su latencia de red
# y la escritura a Supabase, y Render puede estar despertando de dormido.
_MARGEN = 0.80

# .github/workflows/price-tick.yml → curl --max-time 900 por lote.
_CURL_MAX_TIME_S = 900


def _segundos_estimados(n_skins: int) -> float:
    return (n_skins / _history_limiter._limit) * _history_limiter._window


def test_el_lote_cabe_en_lo_que_render_aguanta():
    estimado = _segundos_estimados(PRICE_LOOKUP_CAP)
    assert estimado <= _RENDER_CORTE_S * _MARGEN, (
        f"PRICE_LOOKUP_CAP={PRICE_LOOKUP_CAP} necesita ~{estimado / 60:.1f} min con el "
        f"limiter a {_history_limiter._limit}/{_history_limiter._window:.0f}s. Render "
        f"free cortó a los ~22 min (502) y aguantó 11. Baja el tamaño del lote: "
        f"el workflow ya repite hasta cubrir la población, así que trocear más "
        f"fino no pierde nada."
    )


def test_el_lote_cabe_en_el_curl():
    """El --max-time del curl es la segunda red, por detrás del corte de Render."""
    assert _segundos_estimados(PRICE_LOOKUP_CAP) <= _CURL_MAX_TIME_S * _MARGEN


def test_el_lote_no_es_ridiculo():
    """Lotes minúsculos multiplican las llamadas y el overhead de despertar Render."""
    assert PRICE_LOOKUP_CAP >= 50
