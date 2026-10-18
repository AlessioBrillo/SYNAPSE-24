# Worker Dockerfile — Synapse stateless inference worker.
# Consumes jobs dispatched by Averyn over stored FIT / XDF files.
FROM python:3.11-slim

WORKDIR /app

# System deps needed by some scientific wheels (numpy/scipy build is wheel-based;
# gcc kept for parity with the backend image and optional native builds).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install SYNAPSE-24 package (provides signal_quality, ingestion, utils).
COPY src ./src
RUN pip install --no-cache-dir -e ./src

# Worker code + contract.
COPY worker ./worker
WORKDIR /app/worker

# Entrypoint: `python worker/hrv_worker.py --job ... --file ...`
CMD ["python", "hrv_worker.py"]
