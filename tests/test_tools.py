"""Tests de tools/registry.py — tool registry y execute_tool."""

import pytest
from unittest.mock import AsyncMock

from tools import registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Limpia el registry entre tests para evitar interferencias."""
    registry._tool_registry.clear()
    yield
    registry._tool_registry.clear()


class TestRegisterTool:
    def test_registro_basico(self):
        async def dummy(**kw):
            return "ok"

        registry.register_tool(
            name="test_tool",
            description="Una tool de prueba",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            fn=dummy,
        )
        assert "test_tool" in registry.list_tools()

    def test_declaration_shape(self):
        async def dummy(**kw):
            return "ok"

        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        registry.register_tool(
            name="my_tool",
            description="Desc",
            parameters=schema,
            fn=dummy,
        )
        decls = registry.get_declarations()
        assert len(decls) == 1
        d = decls[0]
        assert d["name"] == "my_tool"
        assert d["description"] == "Desc"
        assert d["parameters"] == schema

    def test_needs_steam_id_flag(self):
        async def dummy(**kw):
            return "ok"

        registry.register_tool(
            name="inv_tool",
            description="Inv",
            parameters={"type": "object", "properties": {}},
            fn=dummy,
            needs_steam_id=True,
        )
        assert registry._tool_registry["inv_tool"]["needs_steam_id"] is True


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_ejecuta_tool_simple(self):
        async def my_fn(**kw):
            return {"answer": 42}

        registry.register_tool(
            name="simple",
            description="Simple",
            parameters={"type": "object", "properties": {}},
            fn=my_fn,
        )
        result = await registry.execute_tool("simple")
        assert result == {"answer": 42}

    @pytest.mark.asyncio
    async def test_pasa_kwargs(self):
        async def my_fn(**kw):
            return kw.get("query")

        registry.register_tool(
            name="with_args",
            description="Args",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            fn=my_fn,
        )
        result = await registry.execute_tool("with_args", query="hello")
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_inyecta_steam_id(self):
        received = {}

        async def my_fn(**kw):
            received.update(kw)
            return "ok"

        registry.register_tool(
            name="needs_sid",
            description="Needs SID",
            parameters={"type": "object", "properties": {}},
            fn=my_fn,
            needs_steam_id=True,
        )
        await registry.execute_tool("needs_sid", steam_id="76561198000000000")
        assert received["steam_id"] == "76561198000000000"

    @pytest.mark.asyncio
    async def test_steam_id_no_expuesto_en_declaration(self):
        """El steam_id NO debe aparecer en las declaraciones de Gemini."""

        async def my_fn(**kw):
            return "ok"

        registry.register_tool(
            name="secret_sid",
            description="Secret",
            parameters={"type": "object", "properties": {}},
            fn=my_fn,
            needs_steam_id=True,
        )
        decls = registry.get_declarations()
        d = next(d for d in decls if d["name"] == "secret_sid")
        assert "steam_id" not in str(d)

    @pytest.mark.asyncio
    async def test_steam_id_faltante_raises(self):
        async def my_fn(**kw):
            return "ok"

        registry.register_tool(
            name="mandatory_sid",
            description="Mandatory",
            parameters={"type": "object", "properties": {}},
            fn=my_fn,
            needs_steam_id=True,
        )
        with pytest.raises(ValueError, match="requiere steam_id"):
            await registry.execute_tool("mandatory_sid")

    @pytest.mark.asyncio
    async def test_tool_inexistente_raises(self):
        with pytest.raises(KeyError):
            await registry.execute_tool("no_existe")


class TestListTools:
    def test_list_tools(self):
        async def dummy(**kw):
            return "ok"

        before = set(registry.list_tools())
        registry.register_tool(
            name="a_tool",
            description="A",
            parameters={"type": "object", "properties": {}},
            fn=dummy,
        )
        after = set(registry.list_tools())
        assert "a_tool" in after - before
