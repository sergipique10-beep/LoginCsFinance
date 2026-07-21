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

# .github/workflows/price-tick.yml → curl --max-time 900
_CURL_MAX_TIME_S = 900
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


def test_el_default_anterior_habria_fallado():
    """400 skins ≈ 22 min: documenta por qué se bajó el default."""
    assert _segundos_estimados(400) > _CURL_MAX_TIME_S
