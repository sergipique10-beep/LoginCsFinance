"""Tool registry para el orquestador de Sharky.

Cada tool se registra con:
  - ``name``: nombre que Gemini usa en functionCall
  - ``description``: descripción que Gemini ve
  - ``parameters``: JSON Schema de los parámetros (lo que Gemini puede enviar)
  - ``fn``: callable async a ejecutar
  - ``needs_steam_id``: si ``True``, se inyecta ``steam_id`` del JWT ocultamente

El ``steam_id`` NUNCA se expone en las declaraciones de Gemini — se inyecta
server-side en ``execute_tool``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("uvicorn.error")

_tool_registry: dict[str, dict] = {}


def register_tool(
    name: str,
    description: str,
    parameters: dict,
    fn: Callable,
    *,
    needs_steam_id: bool = False,
) -> None:
    """Registra una tool en el registry.

    ``parameters`` es el JSON Schema que Gemini ve como parámetros de la función.
    ``needs_steam_id`` controla si ``steam_id`` se inyecta ocultamente al ejecutar.
    """
    _tool_registry[name] = {
        "callable": fn,
        "declaration": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
        "needs_steam_id": needs_steam_id,
    }


def get_declarations() -> list[dict]:
    """Retorna las functionDeclarations para el body de Gemini."""
    return [entry["declaration"] for entry in _tool_registry.values()]


async def execute_tool(
    name: str,
    *,
    steam_id: str | None = None,
    **kwargs: Any,
) -> Any:
    """Ejecuta una tool por nombre.

    Si la tool tiene ``needs_steam_id=True``, el ``steam_id`` se inyecta
    automáticamente desde el caller (router). Lanza ``KeyError`` si la tool
    no existe, ``ValueError`` si falta ``steam_id``.
    """
    entry = _tool_registry[name]
    if entry["needs_steam_id"]:
        if steam_id is None:
            raise ValueError(f"Tool '{name}' requiere steam_id")
        kwargs["steam_id"] = steam_id
    return await entry["callable"](**kwargs)


def list_tools() -> list[str]:
    """Retorna los nombres de las tools registradas (para debugging)."""
    return list(_tool_registry.keys())
