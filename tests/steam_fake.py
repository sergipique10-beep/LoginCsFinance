"""CAL-09: doble de las APIs externas de steam/ para los tests de contrato.

Responde por sufijo de `host + path` sobre `main.app.state.http_client`, que es el
borde real del módulo: así los tests no dependen de dónde viva cada función y siguen
valiendo después de cada issue de STEAM-REFACTOR.
"""
from dataclasses import dataclass, field
from typing import Callable

import httpx


@dataclass
class Route:
    status: int = 200
    json: object = None
    content: bytes | None = None
    headers: dict = field(default_factory=dict)
    exc: Exception | None = None
    fn: Callable[[httpx.Request], httpx.Response] | None = None


class FakeUpstream:
    """`on(sufijo, ...)` registra una respuesta (o `fn(request)` si depende de la
    petición); `calls` guarda cada petición."""

    def __init__(self):
        self.routes: dict[str, Route] = {}
        self.calls: list[httpx.Request] = []

    def on(self, suffix: str, status: int = 200, json=None, *, content: bytes | None = None,
           headers: dict | None = None, exc: Exception | None = None,
           fn: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.routes[suffix] = Route(status, json, content, headers or {}, exc, fn)

    def hits(self, suffix: str) -> list[httpx.Request]:
        return [r for r in self.calls if (r.url.host + r.url.path).endswith(suffix)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        target = request.url.host + request.url.path
        # El sufijo más largo gana: "csfloat/history" antes que "/history".
        for suffix in sorted(self.routes, key=len, reverse=True):
            if target.endswith(suffix):
                r = self.routes[suffix]
                if r.exc is not None:
                    raise r.exc
                if r.fn is not None:
                    return r.fn(request)
                if r.content is not None:
                    return httpx.Response(r.status, content=r.content, headers=r.headers)
                return httpx.Response(r.status, json=r.json, headers=r.headers)
        # Catálogo de ByMykel: vacío pero válido, para que no entre en backoff.
        if request.url.host == "raw.githubusercontent.com":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
