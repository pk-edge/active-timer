FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first so pip install runs on container start
COPY requirements.txt .

# Copy all source code
COPY . .

EXPOSE 8000

# Generous start_period — pip install runs at container startup
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Install Python packages then start the server — all at container runtime
CMD ["sh", "-c", "pip install --upgrade pip && pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
