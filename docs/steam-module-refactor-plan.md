# Refactor plan para el módulo steam

> **Ejecutado (2026-10-04 → 2026-10-05, CLEAN-13..19).** Este documento es el plan original.
> Lo que se hizo de verdad, fase a fase, con lo movido, lo que siguió igual, los desvíos y
> las guardias que lo protegen, está en el plan de implementación
> `docs/superpowers/plans/2026-10-04-steam-refactor-arbol-objetivo.md` (sección «Salida de
> fase» y el resumen de las seis fases al final) y en el diagnóstico/spec
> `docs/superpowers/specs/2026-10-04-steam-refactor-arbol-objetivo-design.md`. El árbol
> resultante está en `CLAUDE.md` («Module structure» y «Dependency order»); el mapa de
> degradaciones, en `docs/features/steam.md`.

## Objetivo

Normalizar, desacoplar y estabilizar el módulo de integración de Steam para que deje de mezclar:

- transporte HTTP,
- adaptadores de API externas,
- transformaciones de payloads,
- cachés globales,
- heurísticas de negocio,
- validación y fallback silencioso,
- y reglas de presentación.

La meta no es una reescritura completa a la vez, sino una refactorización incremental con pruebas de regresión y contratos claros entre capas.

---

## Principios

1. La capa de infraestructura no debe decidir la lógica de negocio.
2. Las APIs externas se adaptan a modelos internos explícitos.
3. Los datos incompletos o erróneos no deben volver a la app como datos “válidos” sin contexto.
4. El caché debe tener contrato y observabilidad.
5. Las reglas basadas en strings deben convertirse en modelos o catálogos explícitos.
6. Cada flujo debe tener pruebas de contrato, no solo pruebas de happy path.

---

## Arquitectura propuesta por paquetes

```text
steam/
├── __init__.py
├── api/
│   ├── __init__.py
│   ├── steam_client.py          # cliente HTTP base + endpoints Steam
│   ├── csfloat_client.py        # cliente csfloat
│   ├── buff_client.py            # cliente Buff
│   ├── static_catalog_client.py # catálogo estático de skins / items
│   └── news_client.py            # feeds RSS / Steam News API
├── domain/
│   ├── __init__.py
│   ├── models.py                 # DTOs / dataclasses / TypedDicts
│   ├── enums.py                  # categorías, providers, wear, etc.
│   ├── validators.py             # validaciones de dominio
│   ├── rules.py                  # reglas de negocio, por ejemplo price plausibility
│   └── normalizers.py            # normalización local, no HTTP
├── mappers/
│   ├── __init__.py
│   ├── item_mapper.py            # mapping item → ItemDTO
│   ├── news_mapper.py            # mapping items de RSS/news → NewsDTO
│   ├── movers_mapper.py          # mapping movers / topmovers → mover DTO
│   └── provider_mapper.py        # mapping proveedores → ProviderDTO
├── services/
│   ├── __init__.py
│   ├── market_service.py         # pricing, movers, market data orchestration
│   ├── inventory_service.py      # inventory normalization
│   ├── news_service.py           # feeds + readability + content assembly
│   ├── catalog_service.py        # imágenes + rareza + item metadata
│   └── fx_service.py             # USD/EUR conversions
├── cache/
│   ├── __init__.py
│   ├── base_cache.py             # contrato del caché
│   ├── history_cache.py          # histórico de precios / stats
│   ├── image_cache.py            # catálogo de imágenes
│   ├── market_cache.py           # lookup price / providers / FX
│   └── cache_policy.py           # TTLs + stale + invalidation
├── errors/
│   ├── __init__.py
│   ├── domain_errors.py          # SourceUnavailable, RateLimited, InvalidPayload
│   └── handling.py              # policy for degraded mode / fallback
├── adapters/
│   ├── __init__.py
│   ├── steam_adapter.py          # adaptador de payload Steam a modelo interno
│   ├── csfloat_adapter.py        # adaptador de payload csfloat a modelo interno
│   ├── buff_adapter.py           # adaptador Buff
│   └── static_catalog_adapter.py # catálogo estático a modelos internos
├── utils/
│   ├── __init__.py
│   ├── strings.py                # parsing/cleaning general
│   ├── dates.py                 # utilidades de fechas
│   └── urls.py                  # normalización de URLs
└── tests/
    ├── test_item_mapping.py
    ├── test_market_service.py
    ├── test_news_service.py
    ├── test_cache_policy.py
    └── test_invalid_payloads.py
```

## Responsabilidades por capa

### 1. api/
Encapsula clientes HTTP y endpoints externos.

Debe devolver payloads crudos o estructuras de respuesta sin lógica de negocio.

Responsabilidades:
- llamar a Steam, csfloat, Buff, feeds y catálogo estático,
- manejar timeouts y status code,
- no interpretar valores semánticos,
- no normalizar item names ni categorías,
- no decidir fallback de negocio.

### 2. domain/
Define el modelo del negocio y las reglas que no dependen del proveedor.

Responsabilidades:
- dataclasses/TypedDicts para Item, MarketProvider, PriceHistoryPoint, NewsItem,
- validadores de campos y rangos,
- reglas para cambios plausibles,
- normalizadores internos (URL, wear, rarity, category, names).

### 3. mappers/
Convierte payloads de cada API externa a modelos internos.

Responsabilidades:
- adaptar `Steam raw -> ItemDTO`,
- adaptar `csfloat raw -> PriceHistoryPoint`,
- adaptar `topmovers raw -> MoversDTO`,
- no hacer cache,
- no hacer reglas de negocio avanzadas,
- no realizar fallback silencioso.

### 4. services/
Orquesta el flujo de negocio.

Responsabilidades:
- coordinar llamadas a API, caché y mapeos,
- aplicar reglas de negocio,
- decidir si un resultado es full/partial/stale/error,
- devolver DTO final al resto de la app.

### 5. cache/
Encapsula la política de almacenamiento temporal.

Responsabilidades:
- TTL por clave,
- stale data vs empty data,
- invalidación,
- observabilidad,
- límites y limpieza.

### 6. errors/
Define las señales de error explícitas del módulo.

Responsabilidades:
- distinguir `SourceUnavailable`, `RateLimited`, `InvalidPayload`, `CacheStale`, etc.,
- informar de degradación y así evitar secretos silenciosos de error.

---

## Flujo de datos recomendado

```text
API externa
   ↓
api/<source>_client.py
   ↓
adapter / mapper
   ↓
validated domain model
   ↓
service orchestration
   ↓
cache policy
   ↓
response DTO to app
```

Regla importante: nunca dejar que la app reciba directamente un payload de Steam o JSON crudo.

---

## Mapa de responsabilidades actual a futuro

### Lo que hoy está en services.py y mappers.py

- fetch de providers → debe ir a `api/`
- parsing de payloads → debe ir a `adapters/` o `mappers/`
- validación de rangos y campos → debe ir a `domain/validators.py`
- reglas de negocio de categorías → `domain/rules.py` o `domain/enums.py`
- caché global → `cache/`
- fallback silencioso → `errors/handling.py` + `service` policies
- clasificación por string “contains X” → catálogo + normalizador

---

## Fases de refactorización

## Fase 0 — Línea base y cobertura mínima

### Objetivo

Estabilizar el comportamiento actual antes de reestructurar.

### Tareas

1. Identificar los flujos críticos del módulo:
   - inventory mapping,
   - price deltas,
   - histories,
   - market lookup,
   - movers,
   - images,
   - news parse,
   - FX rate.
2. Crear tests de regresión para cada flujo:
   - payload normal,
   - payload incompleto,
   - payload con valores fuera de rango,
   - source caída,
   - cache stale.
3. Registrar los fallbacks actuales y decidir qué se conservará.

### Criterio de aceptación

- La suite actual de los flujos críticos está documentada y ejecutada.
- Hay pruebas para un payload incompleto y uno con datos implausibles.
- Se conoce qué comportamientos son “degradación aceptada” y cuáles no.

### Riesgos

- Se puede estar refactorizando sin haber capturado la semántica real.
- La app puede depender de un fallback que la lógica de negocio no había formalizado.

---

## Fase 1 — Separar contratos de integración

### Objetivo

Definir interfaces y contratos explícitos para cada fuente externa.

### Tareas

1. Crear modelos internos principales:
   - `ItemDTO`
   - `PriceHistoryPoint`
   - `MarketProviderDTO`
   - `NewsItemDTO`
   - `MoverDTO`
2. Mover conversions a mappers puros.
3. Dejar que los clientes HTTP solo devuelvan payloads crudos.
4. Definir `SourceUnavailable` y `InvalidPayload`.

### Criterio de aceptación

- Ninguna capa superior recibe dicts de Steam crudos directamente.
- Todas las transformaciones pasan por un modelo interno.
- Cada adapter tiene una responsabilidad clara y un solo conjunto de entradas/salidas.

### Riesgos

- Mover demasiado a la vez sin tests de cobertura.
- Repetición de lógica en varios adapters si no se define un patrón común.

---

## Fase 2 — Reducir y formalizar validación

### Objetivo

Eliminar el “cero/None como respuesta por defecto” silencioso.

### Tareas

1. Crear `domain/validators.py` con validaciones para:
   - fechas,
   - valores de precio,
   - volumen,
   - campos requeridos,
   - ratios plausibles,
   - valores de cadena de texto.
2. Reemplazar conversiones directas por validación explícita.
3. Definir política de resultados:
   - `ok`,
   - `partial`,
   - `stale`,
   - `error`.
4. Reescribir fallbacks para que no se oculten errores.

### Criterio de aceptación

- Si un payload inválido llega, se devuelve error o partial con contexto, no un 0 silencioso.
- Un item con precio implausible ya no se trata como válido.
- La app sabe si un dato viene de una fuente degradada.

### Riesgos

- Algunos clientes pueden estar dependientes del comportamiento silencioso actual.
- Debe decidirse explícitamente si ciertos “no datos” son aceptables en UX.

---

## Fase 3 — Encapsular caché y política de TTL

### Objetivo

Dejar de mantener estado global disperso en varios diccionarios no controlados.

### Tareas

1. Crear `cache/base_cache.py` y `cache_policy.py`.
2. Mover caches actuales a clases con:
   - TTL por tipo,
   - invalidación por clave,
   - estado fresh/stale/empty/error,
   - métricas de tamaño y hit/miss,
   - limpieza periódica.
3. Separar caché de histórico y caché de catálogo.
4. Añadir pruebas de:
   - expiración,
   - stale data,
   - invalid value,
   - hit/miss,
   - mapeo entre clave y valor.

### Criterio de aceptación

- No hay diccionarios globales anónimos en los servicios.
- Todo acceso a caché pasa por una clase con política definida.
- Se puede entender qué TTL corresponde a cada flujo.

### Riesgos

- Que cache stale produzca datos incorrectos durante la transición.
- Necesidad de preservar compatibilidad con las llamadas ya existentes.

---

## Fase 4 — Sacar las reglas “hardcoded” del dominio

### Objetivo

Eliminar reglas basadas en strings y condiciones ad hoc que se vuelven frágiles.

### Tareas

1. Extraer categorías a un catálogo de dominio.
2. Extraer wear names, rarity mappings, tipo de item y provider metadata a modelos o enums.
3. Reemplazar lógica tipo `if 'StatTrak™' in name` por helpers de normalización con modelos.
4. Crear catálogo centralizado para:
   - skins,
   - knives,
   - stickers,
   - keychains,
   - agents,
   - patches,
   - crates.

### Criterio de aceptación

- Los nombres de skin ya no se interpretan con reglas de strings dispersas.
- La lógica de categoría y rareza depende de catálogos y modelos, no de concatenaciones.
- Cambios en los nombres de la API tienen menor impacto.

### Riesgos

- El catálogo estático puede no cubrir todas las variantes.
- Necesidad de mantener compatibilidad con nombres históricos.

---

## Fase 5 — Aislar la orquestación con services

### Objetivo

Que el módulo tenga un único punto de composición del flujo y no varios servicios que hagan todo.

### Tareas

1. Crear services por responsabilidad:
   - `inventory_service.py`
   - `market_service.py`
   - `news_service.py`
   - `catalog_service.py`
   - `fx_service.py`
2. Mover la lógica de llamadas y composición a esos servicios.
3. Mantener una API pequeña y clara por servicio.
4. Dejar que el resto del backend no sepa qué fuente usa cada dato.

### Criterio de aceptación

- El flujo de negocio queda claro en una sola capa.
- Un endpoint de la app no necesita saber si un dato viene de Steam, Buff o catálogo estático.
- El módulo es más testeable.

### Riesgos

- Se puede crear demasiada abstracción y dejar el módulo más complejo.
- La composición debe seguir un patrón simple y realista.

---

## Fase 6 — Observabilidad y saneamiento final

### Objetivo

Convertir el módulo en algo diagnósticable y sostenido.

### Tareas

1. Añadir logs estructurados con contexto por operación.
2. Añadir contadores de:
   - source failure,
   - stale cache,
   - invalid payload,
   - fallback used,
   - partial responses.
3. Eliminar comentarios de diario de trabajo que ya no aportan valor.
4. Revisar y reubicar funciones con lógica que solo sirve a una existencia temporal.

### Criterio de aceptación

- Cada error tiene contexto.
- Cada degradación tiene trazabilidad.
- El código es legible para un desarrollador que entra por primera vez.

---

## Orden recomendado de implementación

1. Fase 0: baseline y tests.
2. Fase 1: contratos de integración.
3. Fase 2: validación y errores explícitos.
4. Fase 3: caché encapsulado.
5. Fase 4: reglas hardcoded -> dominio/catalog.
6. Fase 5: services por responsabilidad.
7. Fase 6: observabilidad y limpieza final.

No se debe saltar la fase 0 ni la fase 1 si el objetivo es mantener la funcionalidad mientras se reestructura.

---

## Desglose de tareas para el agente de implementación

### Objetivo de la sesión

Refactorizar el módulo `steam` sin cambiar el comportamiento funcional del negocio, solo la estructura y la robustez.

### Reglas de implementación

- No mezclar refactor con nuevas features funcionales.
- No añadir lógica nueva salvo la necesaria para estabilidad y contratos.
- Mantener compatibilidad con el contrato actual de la app durante las fases intermedias.
- Cada cambio debe ir acompañado de pruebas del flujo afectado.
- Si un fallback actual es silencioso, hay que convertirlo en una respuesta explícita o una degradación documentada.

### Definition of Done del refactor

- Cada fuente externa tiene un adapter claro.
- Cada DTO tiene validación.
- El caché tiene política y observabilidad.
- Las reglas de negocio ya no están dispersas en strings.
- El módulo queda dividido por responsabilidad.
- Hay tests para payloads válidos, incompletos y no plausibles.

---

## Riesgos concretos a vigilar

- Reintroducir un fallback silencioso que oculta un error de backend.
- Cambiar la forma de los datos sin actualizar los consumidores.
- Dejar un caché con TTLs insuficientes o inconsistentes.
- Mover demasiada lógica a un mismo servicio por comodidad.
- Eliminar validaciones sin sustituirlas por otra capa explícita.

---

## Resultado esperado final

El módulo `steam` debería pasar de ser un conjunto de helpers con mucho estado global y lógica mezclada a una estructura de integración con:

- modelos internos explícitos,
- adaptadores por fuente,
- validación centralizada,
- cache con contrato,
- servicios de dominio por responsabilidad,
- y tests que protejan la semántica del negocio.

Eso convertiría el módulo en algo sostenible, depurable y menos propenso a regressions silenciosas.
