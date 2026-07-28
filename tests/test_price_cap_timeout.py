"""El tope de skins por corrida debe caber en el timeout del cron.

Cada lookup del price-tick pasa por `_history_limiter` (18 req/60 s), así que el
tiempo de una corrida lo fija el número de skins, no la red. El curl del workflow
corta a los 900 s: si el cap permite más skins de las que caben, la corrida muere
a medias — y mal, porque el backend sigue procesando cuando curl ya se ha ido.

`tracked_skins` crece sola con el auto-registro desde /inventory (151 skins frente
a las 50 del seed), así que esto degrada con el tiempo aunque hoy vaya bien.
"""

from settings import PRICE_LOOKUP_CAP
from steam.services import _history_limiter

# .github/workflows/price-tick.yml → curl --max-time 1800
# Subido de 900 con el cap de 200→400: la serie es una fila por skin y día, así
# que la skin que no entra en la corrida pierde el punto de ese día para
# siempre. El cap tiene que cubrir toda la población seguida (~320 y creciendo),
# no rotarla — y eso obliga a ampliar la ventana del curl.
_CURL_MAX_TIME_S = 1800
# Margen sobre el timeout: el limiter es el grueso del tiempo, pero cada skin
# suma su propia latencia de red y la escritura en Supabase.
_MARGEN = 0.80


def _segundos_estimados(n_skins: int) -> float:
    por_ventana = _history_limiter._limit
    ventana = _history_limiter._window
    return (n_skins / por_ventana) * ventana


def test_el_cap_cabe_en_el_timeout_del_cron():
    estimado = _segundos_estimados(PRICE_LOOKUP_CAP)
    assert estimado <= _CURL_MAX_TIME_S * _MARGEN, (
        f"PRICE_LOOKUP_CAP={PRICE_LOOKUP_CAP} necesita ~{estimado:.0f}s con el "
        f"limiter a {_history_limiter._limit}/{_history_limiter._window:.0f}s, y el "
        f"curl corta a los {_CURL_MAX_TIME_S}s. Baja el cap o sube --max-time."
    )


def test_el_cap_no_es_ridiculo():
    """Un cap demasiado bajo tarda días en dar la vuelta a las skins seguidas."""
    assert PRICE_LOOKUP_CAP >= 100


def test_hay_un_techo_y_el_cap_esta_por_debajo():
    """El par (cap, max-time) tiene que dejar margen para crecer.

    Antes este test fijaba que 400 no cabía en los 900 s de entonces. Ya no
    aplica —el timeout es 1800— pero el invariante que protegía sigue vivo:
    subir el cap sin subir --max-time deja la corrida a medias, y encima en
    silencio (el backend sigue procesando cuando curl ya se ha ido).
    """
    techo = int((_CURL_MAX_TIME_S * _MARGEN / _history_limiter._window)
                * _history_limiter._limit)
    assert PRICE_LOOKUP_CAP < techo, (
        f"PRICE_LOOKUP_CAP={PRICE_LOOKUP_CAP} está en el techo ({techo}) para "
        f"--max-time={_CURL_MAX_TIME_S}s. Sube el timeout del workflow."
    )
