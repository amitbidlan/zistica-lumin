# syntax=docker/dockerfile:1.6
# Single-image Synaptic: Python API + Next.js dashboard + Python SDK.
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

# Install the SDK (so users can `pip install` on top and `import synaptic`
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
ENV SYNAPTIC_DATA_DIR=/data
VOLUME ["/data"]

# Entrypoint script
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

WORKDIR /app
EXPOSE 3000 8000

# tini reaps zombies + forwards signals; start.sh manages both processes
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
