"""Tope de usos por tool (chat/agent.py:MAX_USOS_POR_TOOL).

Al no encontrar una skin, el modelo prueba variantes del nombre. Cada intento se
acumula en `contents`: en producción llegó a 28 KB y Gemini devolvió 503. El tope
corta el bucle devolviéndole un error explicativo en vez de ejecutar la búsqueda.
"""

import json

import pytest

from chat.agent import MAX_USOS_POR_TOOL, _run_tool


def _fc(nombre: str, **args) -> dict:
    return {"functionCall": {"name": nombre, "args": args}}


@pytest.fixture
def tool_espia(monkeypatch):
    """Registra las ejecuciones que llegan a pasar el tope."""
    llamadas: list[str] = []

    async def _fake(name, *, steam_id=None, **kwargs):
        llamadas.append(name)
        return {"items": []}

    monkeypatch.setattr("tools.registry.execute_tool", _fake)
    return llamadas


@pytest.mark.asyncio
async def test_permite_hasta_el_tope(tool_espia):
    ctx: dict = {}
    for _ in range(MAX_USOS_POR_TOOL):
        await _run_tool(_fc("buscar_skin", query="x"), client=None, ctx=ctx)

    assert len(tool_espia) == MAX_USOS_POR_TOOL


@pytest.mark.asyncio
async def test_corta_pasado_el_tope(tool_espia):
    ctx: dict = {}
    for _ in range(MAX_USOS_POR_TOOL + 2):
        r = await _run_tool(_fc("buscar_skin", query="x"), client=None, ctx=ctx)

    # La tool deja de ejecutarse...
    assert len(tool_espia) == MAX_USOS_POR_TOOL
    # ...y al modelo se le dice por qué, para que responda en vez de reintentar.
    payload = json.loads(r["functionResponse"]["response"]["result"])
    assert "No pruebes más variantes" in payload["error"]


@pytest.mark.asyncio
async def test_el_tope_es_por_tool_no_global(tool_espia):
    """Agotar buscar_skin no puede bloquear consultar_precio_skin."""
    ctx: dict = {}
    for _ in range(MAX_USOS_POR_TOOL + 1):
        await _run_tool(_fc("buscar_skin", query="x"), client=None, ctx=ctx)

    await _run_tool(_fc("consultar_precio_skin", market_hash_name="AK"), client=None, ctx=ctx)

    assert tool_espia.count("consultar_precio_skin") == 1


@pytest.mark.asyncio
async def test_el_contador_no_se_filtra_entre_peticiones(tool_espia):
    """Cada petición trae su ctx: si no, la segunda nacería con el cupo gastado."""
    ctx_a: dict = {}
    for _ in range(MAX_USOS_POR_TOOL + 1):
        await _run_tool(_fc("buscar_skin", query="x"), client=None, ctx=ctx_a)

    ctx_b: dict = {}
    await _run_tool(_fc("buscar_skin", query="y"), client=None, ctx=ctx_b)

    assert len(tool_espia) == MAX_USOS_POR_TOOL + 1
