#!/bin/bash
# Single-container entrypoint: starts FastAPI on :8000 and the Next.js
# standalone server on :3000. Forwards SIGTERM/SIGINT to both children
# so `docker stop` shuts down cleanly.

set -e

API_PID=""
DASH_PID=""

shutdown() {
    echo "shutting down..."
    [ -n "$API_PID" ]  && kill -TERM "$API_PID"  2>/dev/null || true
    [ -n "$DASH_PID" ] && kill -TERM "$DASH_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    exit 0
}
trap shutdown SIGTERM SIGINT

# 1. Start the API (binds 0.0.0.0:8000 — exposed and reachable for any
#    extra agents the user runs against the container)
cd /app/api
uvicorn main:app --host 0.0.0.0 --port 8000 --log-level info &
API_PID=$!

# 2. Wait for API readiness (up to ~9s) so the dashboard's first proxy
#    request doesn't hit a connection refused
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
        echo "lumin api ready on :8000"
        break
    fi
    sleep 0.3
done

# 3. Start the Next.js standalone server (foreground)
cd /app/dashboard
HOSTNAME=0.0.0.0 PORT=3000 node server.js &
DASH_PID=$!

# 4. Wait for either to exit; on exit, shut down the other
wait -n "$API_PID" "$DASH_PID"
shutdown
