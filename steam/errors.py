"""Errores tipados de las fuentes externas de steam/ (CLEAN-06).

El cliente (`steam/clients/steamwebapi.py`) traduce cada respuesta a uno de estos;
quien llama decide qué HTTP devolver. Antes vivían repartidos: `QuotaExhausted` en
price_capture, `SteamRateLimited` en routes/items y `HistoryBusy` en services.
"""


class UpstreamError(Exception):
    """steamwebapi no dio un 200: `status` es el que devolvió (None si no llegó a
    responder) y `body_excerpt` el principio del cuerpo, para los logs."""

    def __init__(self, status: int | None = None, body_excerpt: str = "",
                 retry_after: float | None = None):
        super().__init__(f"HTTP {status}: {body_excerpt}" if status is not None else body_excerpt)
        self.status = status
        self.body_excerpt = body_excerpt
        self.retry_after = retry_after


class QuotaExhausted(UpstreamError):
    """steamwebapi devolvió 402: cuota mensual agotada (PERF-09).

    Medido 2026-09-26: con la cuota agotada, /item responde 402 en TODAS las
    llamadas hasta el reset (día 10 de cada mes). Seguir iterando el lote es
    gastar minutos de Render en nada, y peor: el resultado parecía normal
    (`errors: N`, workflow en verde) y la captura estuvo 4 días muerta sin aviso.
    """

    def __init__(self, body_excerpt: str = ""):
        super().__init__(402, body_excerpt)


class RateLimited(UpstreamError):
    """429 de steamwebapi: límite por minuto, transitorio, se reintenta (PERF-14).

    No confundir con el 402 (`QuotaExhausted`, cuota mensual agotada): ese no se
    reintenta porque cada intento daría otro 402 hasta el reset del día 10.
    """

    def __init__(self, retry_after: float | None = None, body_excerpt: str = ""):
        super().__init__(429, body_excerpt, retry_after)


class SourceTimeout(UpstreamError):
    """La petición caducó. Aparte de `SourceUnavailable` porque las rutas responden
    504 a uno y 502 al otro. El mensaje es el de httpx."""

    def __init__(self, message: str = ""):
        super().__init__(None, message)


class SourceUnavailable(UpstreamError):
    """Error de red (DNS, conexión rechazada…). El mensaje es el de httpx."""

    def __init__(self, message: str = ""):
        super().__init__(None, message)


class InvalidPayload(Exception):
    """Un 200 con un cuerpo que no es JSON.

    No hereda de `UpstreamError` a propósito: hoy un 200 ilegible da 500 en todos los
    llamadores (CAL-14), y un `except UpstreamError` lo convertiría en 502 sin querer.
    """

    def __init__(self, body_excerpt: str = ""):
        super().__init__(body_excerpt)
        self.status = 200
        self.body_excerpt = body_excerpt


class HistoryBusy(Exception):
    """El limiter del histórico está lleno y el llamador no puede esperar (PERF-03).

    Los crons esperan lo que haga falta (mejor tarde que perder el dato); el chat
    no: una espera de hasta 60 s dentro de una respuesta interactiva es un chat
    muerto. Cancelar `acquire()` es seguro: registra la llamada y devuelve sin
    ningún `await` entre medias, así que una cancelación durante la espera no deja
    un hueco fantasma en la ventana.
    """
