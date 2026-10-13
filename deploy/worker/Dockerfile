# Imagen del worker del VPS (COST-01/02). Contexto de build = raíz del repo (compose.yml),
# pero solo entra `worker/`: el backend de Render no viaja en esta imagen.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin worker
WORKDIR /app
COPY deploy/worker/requirements-worker.txt ./
RUN pip install --require-hashes -r requirements-worker.txt
COPY --chown=worker:worker worker ./worker
USER 10001
# `worker` o `scheduler` (worker/main.py); compose.yml fija cuál en cada servicio.
ENTRYPOINT ["python", "-m", "worker.main"]
CMD ["worker"]
