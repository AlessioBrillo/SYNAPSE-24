# Worker Dockerfile — Synapse stateless inference worker.
# Consumes jobs dispatched by Averyn over stored FIT / XDF files.
# Build context: repository root.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install the synapse24 package + its runtime stack from the root manifest.
COPY pyproject.toml README.md ./
COPY src ./src
COPY sdk/python ./sdk/python
RUN pip install --no-cache-dir -e ./sdk/python && pip install --no-cache-dir -e .

# Worker code + contract.
COPY worker ./worker
COPY config ./config

# Entrypoint: orchestrator (Averyn) overrides --job/--file at runtime.
# Default is a no-op import sanity check (exits 0 when the worker loads).
CMD ["python", "-c", "import worker.hrv_worker; print('synapse-worker ok')"]
