# Backend Dockerfile — SYNAPSE-24 Data Plane API.
# Build context: repository root.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install the synapse24 package + its runtime stack from the root manifest.
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Application code.
COPY backend ./backend
COPY worker ./worker
COPY config ./config

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
