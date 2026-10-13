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
#
# WEB_CONCURRENCY — uvicorn worker processes (default 2 for the 2 vCPU / 4 GB
# single-host target). Per worker: DB pool 3+2 (+1+1 ingest), ~300-400 MB RSS.
# Singleton background loops are leader-elected (app/core/LeaderLock.py), so any
# N is safe; keep N * 7 DB connections under Postgres max_connections.
#
# FORWARDED_ALLOW_IPS — which PEER addresses uvicorn trusts to set
# X-Forwarded-For / X-Forwarded-Proto. Default '*' (unchanged from before the
# rate limiter existed): staging/prod run on ECS where port 8000 is reachable
# ONLY through the ALB, whose node IPs change and can't be listed (uvicorn 0.30
# has no CIDR support). Trusting the ALB's X-Forwarded-For is what gives the
# login/register/enroll limiters and the global limiter the REAL client IP —
# a narrower value would bucket every user under the ALB's private IP and let
# one attacker lock everyone out of login. Only narrow it (exact IPs) for a
# deployment where port 8000 is directly reachable from the internet.
ENV WEB_CONCURRENCY=2 \
    FORWARDED_ALLOW_IPS=*

# Shell form via sh -c + exec: expands ${WEB_CONCURRENCY} while uvicorn still
# runs as PID 1 and receives SIGTERM (graceful shutdown flushes ingest/audit).
# uvicorn reads FORWARDED_ALLOW_IPS from the environment itself.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-2} --proxy-headers"]
