# Worker Dockerfile — Synapse stateless inference worker.
# Consumes jobs dispatched by Averyn over stored FIT / XDF files.
# Build context: repository root.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install the synapse24 package + its runtime stack from the root manifest.
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Worker code + contract.
COPY worker ./worker
COPY config ./config

# Entrypoint: orchestrator (Averyn) overrides --job/--file at runtime.
# Defaults are a no-op sanity job; a missing file surfaces a clean ok=False.
CMD ["python", "worker/hrv_worker.py", "--job", "hrv_trends", "--file", "/app/data/sessions/sample.fit"]
