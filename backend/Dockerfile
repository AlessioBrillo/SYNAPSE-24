# Backend Dockerfile — SYNAPSE-24 Data Plane API.
# Build context: repository root.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install the synapse24 package + its runtime stack from the root manifest.
# src/, sdk/python/ and README.md are required for `pip install -e .` to work:
# - src/synapse24 is the package itself,
# - sdk/python provides the path dependency `synapse-device`,
# - README.md is the declared `readme` of the hatchling build.
COPY pyproject.toml README.md ./
COPY src ./src
COPY sdk/python ./sdk/python
RUN pip install --no-cache-dir -e ./sdk/python && pip install --no-cache-dir -e .

# The HTTP server is not part of the package's runtime dependencies (fastapi/uvicorn live in
# backend/requirements.txt only): without this the CMD below fails with "uvicorn: not found".
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Application code.
COPY backend ./backend
COPY worker ./worker
COPY config ./config
# schemas/: the worker validates its payloads against them (without the directory the
# validation is skipped silently).
COPY contracts ./contracts

EXPOSE 8000

# Inside the container the server must listen on every interface for the port mapping to work;
# who can reach it from outside is decided by the published port (docker-compose.yml maps
# 127.0.0.1:8000 only). Started directly on a host it binds to loopback (SYNAPSE_API_HOST unset).
ENV SYNAPSE_API_HOST=0.0.0.0

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
