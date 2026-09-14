# Handoff — Chat Sharky: diagnóstico del 502 y latencia

Sesión del 2026-09-14. Backend `LoginCsFinance`, feature de chat (`POST /rag/chat`).

Este documento cubre **solo** el trabajo sobre el chat. El estado global del
proyecto (frentes abiertos de los tres repos) vive en `../../docs/` en la raíz.

---

## 1. El 502 de producción

**Síntoma reportado:** el chat devolvía "Ha ocurrido un error al contactar con
el asistente" (`chat-modal.ts:79`, mensaje genérico del frontend).

### Cómo se reprodujo

La API exige Bearer JWT, así que la cadena completa fue:

```bash
# 1. Token de producción (review-login, credenciales en .env)
curl -X POST https://cs-finance-api.onrender.com/auth/review-login \
  -H "Content-Type: application/json" \
  -d '{"user":"<REVIEW_USER>","password":"<REVIEW_PASSWORD>"}'
# → 200, {"access_token": "..."}

# 2. Reproducir el fallo
curl -X POST https://cs-finance-api.onrender.com/rag/chat \
  -H "Content-Type: application/json" -H "Authorization: Bearer <token>" \
  -d '{"message":"hola","history":[]}'
# → 502 {"detail":"El asistente no está disponible ahora mismo"}
```

### Causa raíz

El `502` sale de `chat/router.py:71-73` (rama `httpx.HTTPStatusError`). Llamando
a Gemini directamente con la misma key se vio el error real:

| Modelo | Resultado |
|---|---|
| `gemini-flash-latest` (el que usaba prod) | **503 UNAVAILABLE** — "experiencing high demand", 3/3 intentos |
| `gemini-2.5-flash` | **200**, 5/5 intentos, 0,8 s |
| `gemini-2.5-flash-lite` | 404 — "no longer available to new users" |

Descartes verificados con curl, no por inspección:

- **No era la API key**: una key inválida da `400` en 0,15 s; producción tardaba ~1 s.
- **No era el payload**: 6,7 KB con las 9 tools, muy por debajo del umbral de 15 KB
  documentado en `llm/gemini.py:23`.
- **No eran los embeddings**: `gemini-embedding-001` respondía `200` en 0,4 s.
- **No era Render**: `GET /` respondía en 0,25 s.

**Fix aplicado por el usuario:** `GEMINI_MODEL=gemini-2.5-flash` como variable de
entorno en Render. Confirmado funcionando.

### Deuda que queda de esto

`settings.py:42` **sigue teniendo `gemini-flash-latest` como default en código**.
Los alias `-latest` los mueve Google sin avisar; el arreglo vive solo en el panel
de Render. Si alguien despliega en otro entorno, vuelve el mismo 502.
Recomendación: fijar el default a un ID pineado.

---

## 2. Latencia

### Medido en producción (servicio caliente)

| Mensaje | E2E | Gemini directo | Tools |
|---|---|---|---|
| `"hola"` | 2,5 – 10,9 s | 1,0 s | 0 |
| `"precio de AK-47 Redline"` | **21,0 s** | — | 1 |
| `"qué skins están subiendo hoy?"` | **25,2 s** | — | 1+ |

Control: `GET /` en 0,25 s — la plataforma no es el problema.

### Dónde se va el tiempo, por impacto

**1. Las vueltas de tools, en serie.** `MAX_TOOL_TURNS = 3` (`chat/agent.py:26`).
Cada vuelta es una llamada completa a Gemini que reenvía el input entero
(system prompt + 9 declaraciones de tools ≈ 1.700 tokens) más el `contents`
acumulado. Una pregunta de precio son 2 llamadas mínimo. Aquí está el grueso.

**2. `_history_limiter` puede dormir hasta 60 s.** `steam/services.py:71` limita a
18 req/60 s con `asyncio.sleep`. Las tools `historial_precio` y
`obtener_historico_skin` pasan por él (`services.py:83`). Si la ventana está
llena, la tool **espera**, y esa espera va dentro del tiempo de respuesta del
chat. Es el peor caso y explica la varianza (2,5 s vs 10,9 s para el mismo `"hola"`).

**3. Thinking activo.** `gemini-2.5-flash` lo trae por defecto: 50-109 tokens de
razonamiento medidos **por llamada**, en todas las vueltas.

**4. Sin streaming en ninguna capa.** Ni `streamGenerateContent` en el backend ni
SSE hacia Angular (`steam-api.service.ts:22` es un `post` normal). El usuario ve
la burbuja *pending* hasta que llega la respuesta entera.

### Los tokens de salida NO eran la causa principal

Se midió: input 6,6 KB ≈ 1.700 tokens, salida 270-360 tokens. Una respuesta larga
añade 1-2 s, no veinte. Las tools ya proyectan campos y recortan a 8 items
(`_para_llm`, `tools/market_tools.py`). Esa parte estaba bien hecha.

---

## 3. Cambios aplicados

### `chat/agent.py:261` — thinking desactivado

```python
"generationConfig": {"thinkingConfig": {"thinkingBudget": 0}},
```

En todas las vueltas: con `MAX_TOOL_TURNS = 3` el ahorro se multiplica, y las
reglas del system prompt son instrucciones a seguir, no deducciones.

### `chat/prompts.py` — dos bloques nuevos al principio del prompt

- **`LONGITUD DE LA RESPUESTA`**: 2-4 frases por defecto, sin introducciones ni
  cierres de "¿te ayudo en algo más?".
- **`CUÁNDO USAR HERRAMIENTAS`**: dice explícitamente que cada tool cuesta
  segundos de espera, y enumera qué responder de memoria (quién es, saludos,
  conceptos generales de CS2: float, desgaste, spread). Reserva las tools para
  el dato vivo: precio, movimiento, inventario, predicción, noticia.

Se mantuvo dentro del bloque nuevo la regla "si no tienes datos, dilo en lugar de
inventar" como contrapeso: acotar tools no puede derivar en responder precios de
memoria.

El prompt pasó de 2.620 a 3.386 chars. Es más largo pero se paga una vez por
vuelta; el ahorro esperado es evitar vueltas enteras.

### `tests/test_chat_prompts.py` — 3 tests nuevos

`test_acota_el_uso_de_tools_a_datos_vivos`, `test_pide_respuestas_cortas_por_defecto`
y `test_sigue_permitiendo_tools_para_datos_de_mercado` (este último es el
contrapeso anti-sobrecorrección).

**Suite completa: 226 passed.**

---

## 4. ⚠️ Verificación E2E PENDIENTE

**Los cambios de prompt no se han verificado contra Gemini real.** La API key
entró en `429` durante las mediciones y no se recuperó.

El límite es **20 requests/minuto/día por proyecto y modelo** en free tier
(métrica `generate_content_free_tier_requests`). El mensaje "retry in 47s" es
engañoso: no se recupera esperando ese tiempo.

Queda por confirmar, cuando haya cuota:

| Caso | Esperado |
|---|---|
| `"dime quién eres"` | **sin** tools |
| `"hola qué tal"` | **sin** tools |
| `"qué es el float de una skin?"` | **sin** tools |
| `"cómo funciona el mercado de steam?"` | **sin** tools |
| `"precio de AK-47 Redline"` | **con** tool (control anti-sobrecorrección) |

Script de verificación: construir el body con `get_declarations()` +
`with_rag_context(_SYSTEM_PROMPT_TOOLS, [])`, mandarlo a
`gemini-2.5-flash:generateContent` con `thinkingBudget: 0`, y mirar si la
respuesta trae `functionCall`.

---

## 5. Siguientes pasos recomendados

1. **Verificar E2E** lo de arriba (bloqueado por cuota).
2. **Acotar el `_history_limiter` dentro del chat**: un `asyncio.wait_for` de 2-3 s
   y, si no hay hueco, devolver al modelo un error explicativo como ya se hace en
   `_run_tool`. Un chat que dice "no puedo consultar el histórico ahora" en 3 s es
   mejor que uno que acierta en 60. Ataca el peor caso.
3. **Fijar `GEMINI_MODEL` en código** (`settings.py:42`), no solo en Render.
4. **Streaming** (`streamGenerateContent` + SSE + consumo en Angular): no reduce el
   tiempo total, pero cambia por completo la percepción — primer token en ~1 s en
   vez de pantalla muerta 20 s. Es el cambio más grande de los cuatro.

## 6. El problema de fondo: la cuota

**20 req/día en free tier, y el loop gasta 2-4 llamadas por mensaje.** Un solo
usuario haciendo 5-7 preguntas con tools agota la cuota diaria de toda la app —
y entonces el `502` vuelve, esta vez de verdad.

Los cambios aplicados ayudan (cada pregunta de charla que ya no llama a una tool
es una vuelta menos), pero no lo resuelven. Si Sharky va a producción de verdad,
esto necesita plan de pago. Es la misma causa raíz del incidente que abrió la
sesión.
