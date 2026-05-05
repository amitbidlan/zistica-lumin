#!/bin/bash
# One-shot setup for a fresh Lumin checkout.
#
# What this does:
#   1. Creates a Python venv in packages/api/.venv
#   2. Installs the local Python SDK (editable) — must come first
#      because the API imports `lumin` from there. (The PyPI package
#      named "lumin" is unrelated to this project; never let pip
#      pull from there.)
#   3. Installs the API's requirements
#   4. Runs `npm ci` for every Node package in dependency order
#
# After this runs, you can:
#   - Start everything: ./start-dev.sh         (creates that next)
#   - Run tests:        ./test.sh              (creates that next)
#   - Or work per-package the manual way described in CONTRIBUTING.md

set -e

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

# ANSI colors for readability — harmless on dumb terminals
GREEN=$'\033[0;32m'
BLUE=$'\033[0;34m'
YELLOW=$'\033[1;33m'
RED=$'\033[0;31m'
RESET=$'\033[0m'

step() {
    echo
    echo "${BLUE}▶${RESET} ${1}"
}

ok() {
    echo "  ${GREEN}✓${RESET} ${1}"
}

warn() {
    echo "  ${YELLOW}!${RESET} ${1}"
}

fail() {
    echo "  ${RED}✗${RESET} ${1}" >&2
    exit 1
}


# ----- Prereq checks -----

step "Checking prerequisites"
command -v python3 >/dev/null 2>&1 || fail "python3 not found — install Python 3.10+ first"
command -v node    >/dev/null 2>&1 || fail "node not found — install Node 18+ first"
command -v npm     >/dev/null 2>&1 || fail "npm not found — install Node 18+ first"

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
NODE_VER=$(node -v | sed 's/^v//')
ok "python3 ${PY_VER}"
ok "node ${NODE_VER}"


# ----- Python -----

step "Installing Python SDK + API"
cd "$REPO_ROOT/packages/api"

if [ ! -d .venv ]; then
    python3 -m venv .venv
    ok "created venv at packages/api/.venv"
else
    ok "venv exists, reusing"
fi

# shellcheck disable=SC1091
. .venv/bin/activate

pip install --upgrade pip --quiet 2>&1 | grep -v '^Requirement already' || true
ok "pip upgraded"

# Step 1: local Python SDK in editable mode. Must come before the API
# requirements — `lumin` is imported from this sibling, NOT from PyPI
# (where a different package shares the name). Order matters.
pip install -e ../sdk-python --quiet 2>&1 | tail -3
ok "lumin SDK installed (editable from packages/sdk-python)"

# Step 2: API runtime deps
pip install -r requirements.txt --quiet 2>&1 | tail -3
ok "API requirements installed"

# Step 3: pytest, only used for `./test.sh` and contributors. Not
# strictly required to run the API, so failure here is a warning.
pip install pytest --quiet 2>&1 | tail -3 || warn "pytest not installed (only needed for tests)"
ok "pytest installed"

deactivate


# ----- Node packages -----

# Order:
#   1. sdk-typescript    — produces dist/ that consumers might import
#   2. integrations/*    — depend on @opentelemetry, no internal deps
#   3. dashboard         — top-level UI

NODE_PKGS=(
    "packages/sdk-typescript"
    "packages/integrations/openclaw"
    "packages/integrations/mastra"
    "packages/integrations/voltagent"
    "packages/dashboard"
)

step "Installing Node packages"
for pkg in "${NODE_PKGS[@]}"; do
    cd "$REPO_ROOT/$pkg"
    # `npm ci` requires a lockfile. It's the right call because it
    # installs EXACTLY what the lockfile says — reproducible across
    # machines, faster than `npm install`. If a contributor has
    # legitimately changed package.json, they should re-run `npm install`
    # themselves first to refresh the lockfile.
    npm ci --silent 2>&1 | tail -3 | grep -v "^$" || true
    ok "$pkg"
done


# ----- Done -----

step "Setup complete"
cat <<'EOF'

  Next steps:
    Run the API:        cd packages/api && .venv/bin/uvicorn main:app --port 8000
    Run the dashboard:  cd packages/dashboard && npm run dev
    Run all tests:      see CONTRIBUTING.md for per-package commands

  The API's `lumin` Python import comes from packages/sdk-python (editable).
  Re-run this script after pulling main if package.json or
  requirements.txt has changed.

EOF
