# syntax=docker/dockerfile:1.6
# Single-image Lumin: Python API + Next.js dashboard + Python SDK.
#
# Stage 1 builds the dashboard with Node 20.
# Stage 2 is the runtime: Python 3.12 with a minimal Node runtime for the
# Next.js standalone server, plus the API and SDK.

# ---------- Stage 1: build the dashboard ----------
FROM node:20-bookworm-slim AS dashboard-build

WORKDIR /build
COPY packages/dashboard/package.json packages/dashboard/package-lock.json* ./
# Use ci if a lockfile is present, fall back to install otherwise
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY packages/dashboard/ ./
RUN npm run build

# Standalone output needs static + public copied alongside server.js
RUN cp -r .next/static .next/standalone/.next/static \
 && (cp -r public .next/standalone/public 2>/dev/null || true)

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim

# Node + curl (curl is used by start.sh's health probe).
# Pulling Node 20 from NodeSource keeps it close to the build stage.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg tini \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Python deps for the API
COPY packages/api/requirements.txt /tmp/api-requirements.txt
RUN pip install --no-cache-dir -r /tmp/api-requirements.txt

# Presidio NER model. The presidio-analyzer package itself doesn't
# bundle a spaCy model — its AnalyzerEngine() init fails if no model
# is downloaded. We default to ``en_core_web_lg`` (Presidio-
# recommended) for highest NER accuracy on names / organizations /
# locations. Pulled at build time so the first request after
# container start doesn't trigger a multi-minute download.
#
# Image-size cost: ``_lg`` ~750MB. Smaller alternatives:
#   _md  ~40MB,  ~95% accuracy
#   _sm  ~12MB,  ~90% accuracy
#
# Override via build arg:
#   docker compose build --build-arg LUMIN_PRESIDIO_MODEL=en_core_web_md
# Then set the matching runtime env var on the container so the
# Presidio detector wires the right model name (see presidio.py).
ARG LUMIN_PRESIDIO_MODEL=en_core_web_lg
ENV LUMIN_PRESIDIO_MODEL=${LUMIN_PRESIDIO_MODEL}
RUN python -m spacy download ${LUMIN_PRESIDIO_MODEL}

# Install the SDK (so users can `pip install` on top and `import lumin`
# from inside the container if they want to run agents alongside).
# Include the `anthropic` extra so the Anthropic integration is importable
# out of the box — the langchain/crewai integrations stay opt-in via
# their own pip installs to keep the image small.
COPY packages/sdk-python/ /app/sdk-python/
RUN pip install --no-cache-dir "/app/sdk-python/[anthropic]"

# API source
COPY packages/api/ /app/api/

# Dashboard: copy ONLY the standalone artifacts from the build stage
COPY --from=dashboard-build /build/.next/standalone/ /app/dashboard/

# Persistent data volume target
RUN mkdir -p /data
ENV LUMIN_DATA_DIR=/data
VOLUME ["/data"]

# Entrypoint script
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

WORKDIR /app
EXPOSE 3000 8000

# Container-level health probe. /health is on the API, which start.sh
# brings up before the dashboard, so this hits true once the engine
# is fully ready. start_period: 30s tolerates slow image boots on
# small VPSes (DuckDB schema migrations + classifier loading add
# ~5-15s on a 2-vCPU host).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -sf http://127.0.0.1:8000/health || exit 1

# tini reaps zombies + forwards signals; start.sh manages both processes
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
