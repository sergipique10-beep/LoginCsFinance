---
feature: worker
files:
  - worker/settings.py
  - worker/logs.py
  - worker/broker.py
  - worker/jobs.py
  - worker/main.py
  - worker/health.py
  - worker/net/gate.py
  - worker/net/breaker.py
  - worker/net/retry.py
  - worker/store/redis.py
  - worker/store/pg.py
  - worker/store/supabase.py
---

# Worker del VPS (COST-02)

Proceso aparte del backend de Render: corre en el VPS (`deploy/compose.yml`) y es quien
sustituye a steamwebapi recogiendo precios e inventarios de fuentes gratuitas. **No importa
nada del backend** (la imagen solo lleva `worker/`) y el backend no importa nada de aquí.

## Piezas

| Módulo | Qué hace |
|---|---|
| `broker.py` | Taskiq: `RedisStreamBroker` (streams + consumer group + ack) y `TaskiqScheduler` con `LabelScheduleSource`. Recursos por proceso en `WORKER_STARTUP` (`state.http`, `.redis`, `.gate`, `.breakers`, `.pg`, `.supabase`) |
| `jobs.py` | Los jobs. Hoy solo `noop` (cada minuto: `SELECT 1` en Supabase y en Timescale + push a Kuma) |
| `main.py` | `python -m worker.main worker\|scheduler` → CLI de Taskiq. **Un único `scheduler`** |
| `net/gate.py` | GCRA en Valkey por host (`rl:{host}`), jitter, cooldown tras 429 (`cool:{host}`: 1 h, ×2, tope 6 h) y presupuestos diarios (`budget:{kind}:{yyyymmdd}`) |
| `net/breaker.py` | pybreaker por host con estado en Valkey (`cb:{host}`): 5 fallos → abierto 300 s (Steam 900). Logs `[breaker]` |
| `net/retry.py` | `request()`: **la única puerta a Internet**. Gate → breaker → petición → stamina (3 intentos en 5xx / transporte / 429 sin Retry-After fuera de Steam) |
| `store/` | Clientes de Valkey (async + sync, reconectan con backoff), Timescale (asyncpg) y Supabase (service_role) |
| `health.py` | Push a Uptime Kuma al final de cada job (`KUMA_PUSH_TOKENS=job=token,…`) |
| `logs.py` | `SecretFilter`: ningún secreto (`STEAM_WEB_API_KEY` y demás de `settings.SECRET_ENV`) ni `key=` de una URL en claro |

## Reglas

- **Ninguna petición a Internet sin `retry.request()`.** Un `httpx.get` directo se salta el
  gate y es como Steam acaba bloqueando la IP del VPS.
- **Un host sin entrada en `settings.DEFAULT_LIMITS` no pasa** (`UnknownHost`): un host nuevo
  se declara con su límite, no se cuela con uno por defecto.
- **Un 429 de Steam no se reintenta**: abre cooldown y todas las peticiones a ese host fallan
  rápido (`HostCoolingDown`) hasta que caduca. Un 429 nunca cuenta como caída para el breaker.
- Los límites del diseño (Steam Community 10/min ráfaga 2, Web API 60/min, Skinport 8/5 min)
  son el punto de partida; **se ajustan con las cifras de COST-00** vía `GATE_LIMITS` (JSON)
  en el `.env`, sin tocar código.
- Garantía de la cola: *at-least-once*. Un job encolado durante un corte de Valkey puede
  tardar hasta `idle_timeout` (10 min) en reentregarse; los jobs tienen que ser idempotentes.

## Dependencias

`deploy/worker/requirements-worker.in` (directas) → `requirements-worker.txt` (con hashes,
generado en `python:3.12-slim` con `pip-compile --generate-hashes`; la imagen instala con
`--require-hashes`). CI las instala vía `requirements-dev.txt` para testear `worker/`.

## Verificación hecha (2026-10-13, Docker local)

`noop` cada minuto (scheduler encola, worker ejecuta, Timescale responde); Valkey parado 40 s
y arrancado → el worker reconecta sin reiniciar el contenedor. Tests: GCRA compartido por dos
procesos del SO contra un Redis real, cooldown, presupuestos, breaker compartido entre
instancias, política de reintentos. **Pendiente en el VPS:** push real a Kuma (monitor creado),
Supabase con la service key, y cortar la red 5 min para ver `[breaker]` abrir y cerrar.
