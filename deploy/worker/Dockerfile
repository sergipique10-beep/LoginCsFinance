# Imagen del worker del VPS (COST-01/02). Contexto de build = raíz del repo (compose.yml),
# pero solo entran `worker/` y el dominio puro de `steam/`: el backend de Render no viaja en esta imagen.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin worker
WORKDIR /app
COPY deploy/worker/requirements-worker.txt ./
RUN pip install --require-hashes -r requirements-worker.txt
COPY --chown=worker:worker worker ./worker
# COST-05: los rankings reutilizan las reglas y el mapper de filas del backend para escribir
# exactamente la misma fila que hoy. Solo entra la parte PURA de steam/ (sin HTTP, settings ni stores):
# domain/, mappers/row_mapper.py y utils/urls.py no importan nada fuera de sí mismos.
COPY --chown=worker:worker steam/__init__.py ./steam/
COPY --chown=worker:worker steam/domain ./steam/domain
COPY --chown=worker:worker steam/mappers/__init__.py steam/mappers/row_mapper.py ./steam/mappers/
COPY --chown=worker:worker steam/utils/__init__.py steam/utils/urls.py ./steam/utils/
USER 10001
# `worker` o `scheduler` (worker/main.py); compose.yml fija cuál en cada servicio.
ENTRYPOINT ["python", "-m", "worker.main"]
CMD ["worker"]
