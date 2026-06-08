# syntax=docker/dockerfile:1

# ── Stage 1: install dependencies into an isolated venv ───────────────────────
# All requirements ship prebuilt wheels for slim/linux, so no compiler is needed.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Pull the latest base-OS security patches, then strip apt metadata.
# (No curl / gcc installed — removes the curl, libcurl4t64 and libssh2-1t64 CVEs
#  and the build-toolchain attack surface.)
RUN apt-get update \
 && apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Pre-built virtualenv from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# Application source.
COPY . .

# Drop root.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# curl-free healthcheck — uses only the venv Python + stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"]

# Dependencies are baked into the image; only the server starts at runtime.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
