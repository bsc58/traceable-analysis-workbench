#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${DEMO_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  "${PYTHON_BOOTSTRAP:-python3.12}" -m venv "$ROOT/.venv"
  "$PYTHON" -m pip install pip==25.0.1 setuptools==75.8.0 wheel==0.45.1
  "$PYTHON" -m pip install -r requirements.lock
  "$PYTHON" -m pip install --no-deps --no-build-isolation -e .
fi
if [[ ! -d apps/web/node_modules ]]; then npm --prefix apps/web ci --ignore-scripts --no-audit --no-fund; fi
if [[ ! -f apps/web/dist/index.html ]]; then npm --prefix apps/web run build; fi
exec "$PYTHON" scripts/demo_runtime.py local "$@"
