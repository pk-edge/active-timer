# syntax=docker/dockerfile:1

# ── Stage 1: build ────────────────────────────────────────────────────────────
# Full build toolchain lives here only; none of it reaches the runtime image.
FROM node:20-slim AS builder

ARG NEXT_PUBLIC_API_URL=http://localhost:8000
# NEXT_PUBLIC_* values are inlined into the client bundle at build time.
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_TELEMETRY_DISABLED=1

WORKDIR /app

# Install deps as a cached layer (only re-runs when the lockfile changes).
COPY package.json package-lock.json ./
RUN npm ci

# Build the standalone server bundle (.next/standalone).
COPY . .
RUN npm run build

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
# Minimal Debian-slim base — no compilers, ImageMagick, MariaDB/Postgres dev
# libs, etc. Only the traced runtime deps from `output: "standalone"` ship here.
FROM node:20-slim AS runner

# Apply the latest base-OS security patches, then remove the bundled npm.
# The standalone server runs via `node server.js` and never needs npm, so
# deleting it removes the cross-spawn / glob / minimatch / tar CVEs that ship
# inside npm's own dependency tree.
RUN apt-get update \
 && apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/* \
 && rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

WORKDIR /app

# Copy only what the standalone server needs, owned by the unprivileged user.
COPY --from=builder --chown=node:node /app/public ./public
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static

# Drop root — `node` (uid 1000) ships with the base image.
USER node

EXPOSE 3000

# Self-contained server emitted by Next.js standalone output.
CMD ["node", "server.js"]
