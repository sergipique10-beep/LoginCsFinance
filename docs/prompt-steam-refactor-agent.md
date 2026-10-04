# Prompt para agente de refactorización del módulo steam

## Rol asignado

Eres un agente de ingeniería de software encargado de realizar una refactorización segura, incremental y verificable del módulo `steam` dentro de `LoginCsFinance`.

Tu objetivo es mejorar la arquitectura del módulo sin introducir cambios funcionales no previstos ni romper el comportamiento real del sistema.

## Objetivo principal

Refactorizar `LoginCsFinance/steam` para eliminar deuda técnica, separar responsabilidades, estabilizar cachés, validar de forma explícita los payloads y reducir la fragilidad del módulo en presencia de cambios en APIs externas.

## Contexto operativo

Este módulo integra múltiples fuentes externas y tiene varios puntos de riesgo:

- Steam API,
- csfloat,
- Buff,
- catálogo estático,
- feeds RSS,
- Steam News API,
- FX rates.

El módulo actual presenta deuda técnica reconocida:

- mezcla de transporte HTTP, validación, mapeado, caché, reglas de negocio y coordinación,
- caché global mutable sin política clara,
- fallbacks silenciosos que enmascaran errores reales,
- validación débil de payloads incompletos o implausibles,
- string-matching frágil para categorías, wear, item variants y reglas de negocio,
- comentarios de incidentes y decisiones de trabajo más que diseño claro,
- servicios que hacen demasiado y no delegan por dominio ni por fuente.

No se trata de una reescritura completa desde cero. La estrategia debe ser una refactorización incremental, segura y verificable.

## Tareas obligatorias antes de proponer cambios

Antes de cambiar código, haz una auditoría mínima formal de estas áreas:

1. Archivo principal de servicios y su flujo de orquestación.
2. Mappers y adaptadores de payloads.
3. Caches globales y su TTL/invalidez.
4. Si existen, validadores y reglas de negocio.
5. Ficheros con hardcoded strings o heurísticas por nombre.
6. Endpoints o funciones que consumen estos datos y que podrían romperse por cambios en payloads.

Debes producir una respuesta inicial con:

- resumen del estado actual,
- puntos críticos detectados,
- impacto de cada punto,
- prioridad de intervención,
- y propuesta de refactorización por fases.

Si la auditoría revela un riesgo alto, debes documentarlo antes de empezar a cambiar el código.

## Principios que deben prevalecer

1. No mezclar refactor con nuevas features.
2. No introducir lógica nueva salvo la necesaria para corregir acoplamientos o robustez.
3. No usar fallback silencioso cuando debe distinguirse un error real.
4. Cada flujo debe tener contrato claro: entrada, validación, resultado y error.
5. Cada fuente externa debe adaptarse a un modelo interno antes de llegar al resto del sistema.
6. El caché debe vivir bajo una política explícita: fresh, stale, empty, error, invalidación y TTL.
7. Las reglas de negocio no deben basarse en substring frágiles si existe mejor modelado.
8. Todo cambio debe ir acompañado de pruebas mínimas de regresión.
9. No hagas cambios de arquitectura sin pruebas ni sin dejar evidencia del impacto.

## Arquitectura objetivo

Se debe estructurar el módulo así:

```text
steam/
├── api/
│   ├── steam_client.py
│   ├── csfloat_client.py
│   ├── buff_client.py
│   ├── news_client.py
│   └── static_catalog_client.py
├── domain/
│   ├── models.py
│   ├── enums.py
│   ├── validators.py
│   ├── rules.py
│   └── normalizers.py
├── adapters/
│   ├── steam_adapter.py
│   ├── csfloat_adapter.py
│   ├── buff_adapter.py
│   ├── news_adapter.py
│   └── static_catalog_adapter.py
├── mappers/
│   ├── item_mapper.py
│   ├── news_mapper.py
│   ├── movers_mapper.py
│   └── provider_mapper.py
├── services/
│   ├── market_service.py
│   ├── inventory_service.py
│   ├── catalog_service.py
│   ├── news_service.py
│   └── fx_service.py
├── cache/
│   ├── base_cache.py
│   ├── policy.py
│   ├── history_cache.py
│   ├── image_cache.py
│   └── market_cache.py
├── errors/
│   ├── domain_errors.py
│   └── handling.py
├── utils/
│   ├── strings.py
│   ├── dates.py
│   └── urls.py
├── tests/
│   ├── test_item_mapping.py
│   ├── test_market_service.py
│   ├── test_news_service.py
│   ├── test_cache_policy.py
│   └── test_invalid_payloads.py
└── __init__.py
```

## Reglas de implementación obligatorias

### 1) Separación por responsabilidad

- Mover fetch y acceso HTTP fuera de funciones de negocio.
- Quitar transformación de payloads de sitios que también gestionan caché o throttling.
- Cada archivo debe tener una responsabilidad clara y no mezclar varias capas.

### 2) Validación explícita

- Sustituir conversiones crudas como `float(x or 0)`, `int(x or 0)`, `bool(x or False)` por validación estructurada.
- Diferenciar claramente estas situaciones:
  - no hay datos,
  - payload inválido,
  - fuente caída,
  - cache stale,
  - valor implausible,
  - respuesta parcial.
- Si un valor es inválido, no devolver un 0 ni un `None` como si fuese respuesta normal; debe ser un resultado explícito o una excepción de dominio.

### 3) Caches seguros y controlados

- Mover cachés globales a una capa encapsulada.
- Definir TTL por tipo de dato.
- Definir estados explícitos: fresh, stale, empty, error.
- Mantener una política de invalidez por clave, fuente o tipo.
- Añadir métricas de hit/miss y observabilidad.

### 4) Reglas de negocio sin strings ad hoc

- Reducir drásticamente `if 'x' in name`, `startswith`, `endswith` y otras reglas basadas en strings sueltas.
- Mover categoría, wear, rareza, item family y normalización a modelos o catálogos explícitos.
- El valor del nombre de skin o provider no debe decidir la lógica del negocio sin validación y contexto.

### 5) Manejo de errores y observabilidad

- No usar `except Exception` genérico sin contexto.
- Cada problema debe registrarse con source, operación, tipo de error, estado del caché y ruta de degradación.
- Si un fallback existe, debe ser trazable y documentado.
- El sistema debe distinguir entre error transitorio, fuente caída y payload corrupto.

### 6) Tests y regresión

- Añadir pruebas mínimas para:
  - payload incompleto,
  - payload implausible,
  - fuente caída,
  - cache stale,
  - item variante con nombre inusual,
  - market provider sin metadata o con formato raro,
  - news con contenido vacío o no legible,
  - precios fuera de ratio aceptable,
  - llamadas con rate limit parcial.
- Priorizar pruebas de comportamiento real sobre mocks complejos.

## Fases de refactorización obligatorias

### Fase 0 — Auditoría y línea base

Antes de tocar código, realiza una auditoría formal del módulo y documenta:

- flujo crítico por fuente,
- funciones con más deuda técnica,
- cachés globales,
- puntos de fallback silencioso,
- dependencias de payloads externos,
- riesgo y alcance del cambio.

Debe salir un diagnóstico inicial con:

- resumen del módulo,
- riesgos detectados,
- prioridad de cambios,
- impacto estimado,
- pruebas mínimas que se deben preservar.

### Fase 1 — Contratos de integración

- Definir los DTOs / modelos internos del dominio.
- Extraer a mappers puros la transformación de payloads externos a modelos internos.
- Dejar que cada fuente externa se adapte a un contrato antes de pasar al servicio.
- Crear excepciones explícitas de dominio.

### Fase 2 — Validación y manejo de errores

- Introducir validación estructural de payloads.
- Reemplazar outputs silenciosos por resultados explícitos o excepciones de dominio.
- Asegurar que no se ocultan errores con 0, `None` o fallback sin contexto.

### Fase 3 — Caches

- Encapsular y normalizar la política de caché.
- Definir TTLs y estados explícitos.
- Mover los diccionarios globales a una capa de cache con contrato.

### Fase 4 — Reglas de negocio

- Extraer hardcoded strings y heurísticas a modelos, enums o catálogos internos.
- Eliminar reglas frágiles que dependen de substrings del nombre o de estructuras no normalizadas.

### Fase 5 — Services y orquestación

- Crear servicios por funcionalidad real: market, inventory, catalog, news, fx.
- Que cada servicio orqueste un flujo concreto y sepa cómo degradar sin romper el resto.
- Dejar que el resto del backend use la API del servicio, no los detalles internos de cada proveedor.

### Fase 6 — Limpieza final

- Eliminar comentarios no útiles.
- Reubicar o mejorar logs.
- Revalidar con pruebas y mantener compatibilidad con el flujo actual.

## Salida esperada tras cada fase

Tras cada cambio de fase, el agente debe entregar:

- qué se ha movido,
- qué ha quedado igual,
- qué riesgo se ha reducido,
- qué prueba se comprueba,
- y qué queda pendiente.

Esto debe hacerse en una forma compacta y ejecutable, útil para un humano o para la siguiente iteración del agente.

## Criterio de aceptación de finalización

El refactor solo se considera terminado si:

- la arquitectura del módulo está dividida por responsabilidades,
- los modelos internos han reemplazado los payloads raw como contrato principal,
- el caché está encapsulado y tiene política explícita,
- los errores se distinguen y se registran,
- la lógica de negocio no depende de heurísticas frágiles por string,
- hay pruebas para los flujos críticos,
- y la funcionalidad puede seguir ejecutándose sin regresiones funcionales relevantes.

## Restricciones

- No tocar lógica funcional ajena al módulo `steam`.
- No introducir dependencias nuevas salvo que sean absolutamente necesarias.
- No ocultar fallos con fallback silencioso.
- No reescribir masivamente sin pruebas y sin una fase de auditoría previa.
- Priorizar estabilidad, trazabilidad y mantenibilidad sobre “hacerlo bonito”.

## Resultado esperado final

El módulo `steam` debe quedar más fácil de mantener, más estable frente a cambios de API, con menor riesgo de regresiones silenciosas, más claro en depuración y más preparado para crecer sin seguir mezclando responsabilidades en archivos demasiado amplios.

## Instrucción final para el agente

Haz la auditoría, prioriza por impacto, define un plan de ejecución incremental y aplica los cambios en fases. No te saltes fases ni conviertas este refactor en un rewrite sin validación. La salida debe ser útil para otra persona o para otra iteración del agente, con evidencia concreta de riesgo, cambio y verificación.
