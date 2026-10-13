# Imagen del worker (COST-01). El código y su arranque llegan en COST-02: hasta entonces el
# servicio vive tras el perfil `worker` de compose.yml y no se levanta con un `up` normal.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin worker
WORKDIR /app
COPY requirements-worker.txt ./
RUN pip install -r requirements-worker.txt
COPY --chown=worker:worker . .
USER 10001
CMD ["python", "-c", "print('worker: sin código hasta COST-02')"]
