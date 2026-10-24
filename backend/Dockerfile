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

# Application code.
COPY backend ./backend
COPY worker ./worker
COPY config ./config

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
