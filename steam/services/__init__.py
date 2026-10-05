"""Servicios de steam/, uno por responsabilidad. Orquestan clientes, adapters,
mappers y cachés; no saben de FastAPI: lanzan los errores de `steam/errors/` y las
rutas los traducen a HTTP.

Entre módulos se llaman a través del módulo (`catalog.fetch_static_images(...)`),
no importando la función: así un test puede sustituirla en un solo sitio.
"""
