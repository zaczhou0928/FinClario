#!/bin/sh
set -eu
SERVICE_ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
PROJECT_ROOT=$(dirname "$SERVICE_ROOT")
PYTHON_BIN=${ECHOMIND_PYTHON:-$PROJECT_ROOT/.venv/bin/python}
if [ ! -x "$PYTHON_BIN" ]; then
  printf '%s\n' '请先按 README 安装 .venv，或设置 ECHOMIND_PYTHON。' >&2
  exit 1
fi
cd "$SERVICE_ROOT"
export ECHOMIND_MODE=demo ECHOMIND_STORAGE=local PROMETHEUS_PORT=0
exec "$PYTHON_BIN" -m uvicorn api.main:app --host 127.0.0.1 --port "${API_PORT:-8000}"
