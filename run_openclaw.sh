#!/usr/bin/env bash
set -euo pipefail

# OpenClaw runner for market-signal-lab
# - Loads environment variables from .env.openclaw (if present)
# - Then executes the requested command using the project's venv python
#
# Usage examples:
#   ./run_openclaw.sh main.py --mode pre_market --queue-entry
#   ./run_openclaw.sh monitor.py
#   ./run_openclaw.sh -m core.evolve_strategy

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

ENV_FILE="$ROOT_DIR/.env.openclaw"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  # Always source with absolute path to avoid depending on cwd
  source "$ENV_FILE"
  set +a
fi

# Pick venv python across platforms
if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PY="$ROOT_DIR/venv/bin/python"
elif [[ -x "$ROOT_DIR/venv/Scripts/python.exe" ]]; then
  PY="$ROOT_DIR/venv/Scripts/python.exe"
elif [[ -x "$ROOT_DIR/venv/Scripts/python" ]]; then
  PY="$ROOT_DIR/venv/Scripts/python"
else
  echo "ERROR: venv python not found under $ROOT_DIR/venv"
  echo "Linux:  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
  echo "Windows: python -m venv venv && ./venv/Scripts/pip install -r requirements.txt"
  exit 1
fi

exec "$PY" "$@"
