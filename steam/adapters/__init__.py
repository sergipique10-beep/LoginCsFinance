"""Adapters de steam/: del JSON crudo de cada fuente al modelo interno
(`steam/domain/models.py`), ya validado.

Contrato de cada función: entrada `Any` (lo que devolvió `api/*`), salida un modelo
interno, errores `UnexpectedPayload` (la forma no es la esperada: un dict donde va una
lista) o `InvalidField` (un campo con un tipo imposible). Sin HTTP, sin caché, sin log,
sin fallback: un campo ausente o no convertible es `None` en el modelo, y quien lo
consume decide qué significa. Los mappers (`steam/mappers/`) convierten estos modelos a
los TypedDict de salida.
"""
