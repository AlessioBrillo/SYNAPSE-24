# Backend Dockerfile — SYNAPSE-24 Data Plane API.
# Build context: repository root (see docker-compose.yml).
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python runtime deps (FastAPI, uvicorn, pydantic, pylsl, pyserial, pyyaml, python-dotenv,
# numpy + the full synapse24 scientific stack comes via `pip install -e ./src`).
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# SYNAPSE-24 package (signal_quality, sessions, report, utils, ingestion, ...).
COPY src ./src
RUN pip install --no-cache-dir -e ./src

# Worker package (job_contract + hrv_worker) + backend app.
COPY worker ./worker
COPY backend/main.py ./main.py
COPY config ./config

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
