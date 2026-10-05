"""Enumeraciones del dominio de steam/ (CLEAN-15; la Fase 4 añade Market, Wear...).

Son `Literal` y no `Enum` a propósito: `Fetched.status` y `log_degraded` se comparan y
se escriben como strings en services, rutas y tests, y un `Enum` obligaría a tocar
todos los sitios sin cambiar nada observable. Lo que importa es que las dos listas
vivan en un solo sitio y que `served_to_status` (`steam/errors/handling.py`) sea la
única correspondencia entre ellas.
"""
from typing import Literal

# Estado de un `Fetched[T]`: `ok` (dato bueno), `stale` (caché caducada), `partial`
# (respaldo incompleto: topmovers, proveedores estáticos) o `error` (vacío o nada).
FetchStatus = Literal["ok", "partial", "stale", "error"]

# Qué se sirvió en una degradación, para la línea `[steam-degraded] served=`.
Served = Literal["stale", "empty", "fallback", "error"]
