"""Tests de chat/agent.py: generate_with_tools — flujo function calling mockeado."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from chat import agent
from llm import gemini as llm_gemini


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(llm_gemini, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm_gemini, "GEMINI_MODEL", "gemini-flash-latest")
    # Estos tests ejercitan el loop de tools, no el retrieval: sin precarga RAG
    # la secuencia de responses mockeados corresponde 1:1 con las llamadas.
    monkeypatch.setattr("settings.CHAT_RAG_PRELOAD", False)


def _mock_client(response_data: dict) -> MagicMock:
    """Crea un mock de httpx.AsyncClient que devuelve un solo response."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=response_data)
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    return client


def _mock_client_sequence(responses: list[dict]) -> MagicMock:
    """Crea un mock que devuelve múltiples responses en secuencia."""
    client = MagicMock()
    client.post = AsyncMock(side_effect=[
        MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value=r))
        for r in responses
    ])
    return client


class TestGenerateWithTools:
    @pytest.mark.asyncio
    async def test_respuesta_texto_directo(self):
        """Gemini devuelve texto sin pedir tools."""
        client = _mock_client({
            "candidates": [{"content": {"parts": [{"text": "Hola, soy Sharky."}]}}]
        })
        result = await agent.generate_with_tools(
            client, "hola", [], tools=None,
        )
        assert result == "Hola, soy Sharky."

    @pytest.mark.asyncio
    async def test_function_call_ejecuta_tool(self):
        """Gemini pide functionCall → se ejecuta → segunda llamada devuelve texto."""
        fc = {"name": "test_tool", "args": {"query": "AK Redline"}}
        tool_result = {"price": 12.5}

        client = _mock_client_sequence([
            # Primera llamada: Gemini pide functionCall
            {"candidates": [{"content": {"parts": [{"functionCall": fc}]}}]},
            # Segunda llamada: Gemini redacta la respuesta
            {"candidates": [{"content": {"parts": [{"text": "La AK Redline cuesta $12.5"}]}}]},
        ])

        async def fake_tool(*args, **kw):
            return tool_result

        with patch("tools.registry.execute_tool", side_effect=fake_tool) as mock_exec:
            result = await agent.generate_with_tools(
                client, "cuánto vale?", [], tools=[{"name": "test_tool"}],
            )

        assert result == "La AK Redline cuesta $12.5"
        mock_exec.assert_called_once()
        call_kwargs = mock_exec.call_args
        assert call_kwargs[0][0] == "test_tool"  # positional: name

    @pytest.mark.asyncio
    async def test_steam_id_inyectado_ocultamente(self):
        """El steam_id se pasa vía tool_context, no en los args de Gemini."""
        fc = {"name": "ver_inventario", "args": {}}

        client = _mock_client_sequence([
            {"candidates": [{"content": {"parts": [{"functionCall": fc}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "Tu inventario tiene 5 skins."}]}}]},
        ])

        async def fake_tool(*args, **kw):
            assert kw.get("steam_id") == "76561198000000000"
            return [{"name": "AK Redline"}]

        with patch("tools.registry.execute_tool", side_effect=fake_tool) as mock_exec:
            result = await agent.generate_with_tools(
                client, "ver mi inventario", [],
                tools=[{"name": "ver_inventario"}],
                tool_context={"steam_id": "76561198000000000"},
            )

        assert "inventario" in result.lower()
        # Verificar que steam_id NO está en el body enviado a Gemini
        call_args = client.post.call_args_list
        for call in call_args:
            body = call.kwargs.get("json") or call[1].get("json")
            if body:
                assert "76561198000000000" not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_tool_error_devuelve_texto(self):
        """Si la tool falla, se devuelve un mensaje de error (sin segunda llamada a Gemini)."""
        fc = {"name": "bad_tool", "args": {}}

        client = _mock_client_sequence([
            {"candidates": [{"content": {"parts": [{"functionCall": fc}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "No pude obtener los datos."}]}}]},
        ])

        with patch("tools.registry.execute_tool", side_effect=KeyError("bad_tool")):
            result = await agent.generate_with_tools(
                client, "algo", [], tools=[{"name": "bad_tool"}],
            )

        # El error se devuelve como texto directo (sin 2ª llamada a Gemini)
        assert "bad_tool" in result
        assert client.post.call_count == 1  # solo la 1ª llamada

    @pytest.mark.asyncio
    async def test_sin_candidates_raises(self):
        client = _mock_client({"candidates": []})
        with pytest.raises(RuntimeError, match="posible bloqueo"):
            await agent.generate_with_tools(client, "x", [])

    @pytest.mark.asyncio
    async def test_sin_api_key_raises(self):
        llm_gemini.GEMINI_API_KEY = ""
        with pytest.raises(RuntimeError, match="no configurada"):
            await agent.generate_with_tools(MagicMock(), "x", [])

    @pytest.mark.asyncio
    async def test_thought_signature_preserved(self):
        """El thoughtSignature de Gemini se preserva al reenviar el functionCall."""
        fc_with_sig = {
            "functionCall": {"name": "test_tool", "args": {"q": "x"}},
            "thoughtSignature": "encrypted_sig_abc123",
        }

        client = _mock_client_sequence([
            {"candidates": [{"content": {"parts": [fc_with_sig]}}]},
            {"candidates": [{"content": {"parts": [{"text": "Listo"}]}}]},
        ])

        async def fake_tool(*args, **kw):
            return {"ok": True}

        with patch("tools.registry.execute_tool", side_effect=fake_tool):
            await agent.generate_with_tools(
                client, "test", [], tools=[{"name": "test_tool"}],
            )

        # La segunda llamada debe incluir el part completo con thoughtSignature
        second_call_body = client.post.call_args_list[1].kwargs["json"]
        model_turn = second_call_body["contents"][-2]  # penúltimo turno (model)
        part_sent = model_turn["parts"][0]
        assert part_sent["thoughtSignature"] == "encrypted_sig_abc123"
        assert part_sent["functionCall"]["name"] == "test_tool"


class TestGenerateWithSourcesPertinencia:
    """UX-15: el preload solo se cita si la respuesta salió de él."""

    _PRELOAD = [{"content": "Parche", "title": "Update 2022", "url": "https://x/1"}]

    @pytest.fixture(autouse=True)
    def _preload(self, monkeypatch):
        async def _fake(client, message, history=None):
            return list(self._PRELOAD)
        monkeypatch.setattr(agent, "_retrieve_context", _fake)

    @pytest.mark.asyncio
    async def test_respuesta_por_tool_no_cita_el_preload(self):
        fc = {"name": "predecir_precio", "args": {"skin": "AK"}}
        client = _mock_client_sequence([
            {"candidates": [{"content": {"parts": [{"functionCall": fc}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "Subirá un 3 %"}]}}]},
        ])

        async def fake_tool(*a, **kw):
            return {"delta": 3}

        with patch("tools.registry.execute_tool", side_effect=fake_tool):
            texto, fragmentos = await agent.generate_with_sources(
                client, "¿merece la pena?", [], tools=[{"name": "predecir_precio"}],
            )
        assert texto == "Subirá un 3 %"
        assert fragmentos == []

    @pytest.mark.asyncio
    async def test_respuesta_desde_el_contexto_si_cita(self):
        client = _mock_client({
            "candidates": [{"content": {"parts": [{"text": "Según el parche..."}]}}]
        })
        _, fragmentos = await agent.generate_with_sources(client, "novedades?", [])
        assert fragmentos == self._PRELOAD

    @pytest.mark.asyncio
    async def test_la_tool_rag_si_cita_lo_que_trajo(self):
        """El sink cuenta aunque no hubiera preload útil: el modelo lo pidió."""
        fc = {"name": "buscar_contexto_rag", "args": {"query": "cache"}}
        client = _mock_client_sequence([
            {"candidates": [{"content": {"parts": [{"functionCall": fc}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "Cache vuelve"}]}}]},
        ])

        async def fake_tool(name, *a, _sources_sink=None, **kw):
            _sources_sink.append({"title": "Cache", "url": "https://x/9"})
            return {"encontrado": True}

        with patch("tools.registry.execute_tool", side_effect=fake_tool):
            _, fragmentos = await agent.generate_with_sources(
                client, "¿y Cache?", [], tools=[{"name": "buscar_contexto_rag"}],
            )
        assert [f["url"] for f in fragmentos] == ["https://x/1", "https://x/9"]
