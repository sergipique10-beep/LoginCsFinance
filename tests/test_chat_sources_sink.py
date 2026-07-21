"""Los chunks recuperados vía tool también deben salir en `sources[]`.

Antes solo contaban los del preload: si el modelo llamaba a `buscar_contexto_rag`,
la respuesta citaba noticias reales pero llegaba al frontend sin una sola fuente.
"""

import pytest

from tools.rag_tools import _buscar_contexto_rag

_CHUNKS = [
    {"content": "Cache vuelve al Active Duty.", "title": "CS2 Update",
     "url": "https://example.com/a", "similarity": 0.81},
    {"content": "Season 5 arranca.", "title": "Season 5",
     "url": "https://example.com/b", "similarity": 0.77},
]


@pytest.fixture
def retrieve_ok(monkeypatch):
    async def _fake(client, query, k=3):
        return _CHUNKS[:k]
    monkeypatch.setattr("rag.retrieval.retrieve", _fake)


@pytest.mark.asyncio
async def test_la_tool_alimenta_el_sink(retrieve_ok):
    sink: list[dict] = []
    r = await _buscar_contexto_rag(query="novedades", client=None, _sources_sink=sink)

    assert r["encontrado"] is True
    assert [c["url"] for c in sink] == ["https://example.com/a", "https://example.com/b"]


@pytest.mark.asyncio
async def test_sin_sink_no_rompe(retrieve_ok):
    """El sink es opcional: otros callers usan la tool sin acumulador."""
    r = await _buscar_contexto_rag(query="novedades", client=None)
    assert r["encontrado"] is True


@pytest.mark.asyncio
async def test_sin_resultados_no_ensucia_el_sink(monkeypatch):
    async def _vacio(client, query, k=3):
        return []
    monkeypatch.setattr("rag.retrieval.retrieve", _vacio)

    sink: list[dict] = []
    r = await _buscar_contexto_rag(query="nada", client=None, _sources_sink=sink)

    assert r["encontrado"] is False
    assert sink == []


@pytest.mark.asyncio
async def test_el_modelo_no_ve_las_urls(retrieve_ok):
    """Las copia de memoria y cambia dígitos (visto en E2E): viajan en sources[]."""
    sink: list[dict] = []
    r = await _buscar_contexto_rag(query="novedades", client=None, _sources_sink=sink)

    assert all("url" not in f for f in r["fragmentos"])
    # Pero el sink sí las conserva, que es de donde sale sources[].
    assert all(c.get("url") for c in sink)
