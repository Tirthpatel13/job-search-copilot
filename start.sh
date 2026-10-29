#!/usr/bin/env bash
# One-click start for Mac and Linux: sets everything up on first run, then opens the app.
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.11+ is required. Install it from https://www.python.org/downloads/ and run this again."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "First run: creating a private Python environment in .venv ..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Reinstall only when requirements.txt changes.
if ! cmp -s requirements.txt .venv/.installed-requirements 2>/dev/null; then
  echo "Installing dependencies (a few minutes the first time) ..."
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
  cp requirements.txt .venv/.installed-requirements
fi

[ -f .env ] || cp .env.example .env

URL="http://localhost:8000"
echo "Starting Job Copilot at $URL (press Ctrl+C to stop) ..."
( sleep 3; (command -v open >/dev/null && open "$URL") || (command -v xdg-open >/dev/null && xdg-open "$URL") || true ) >/dev/null 2>&1 &
exec python -m jobcopilot.cli serve --host 127.0.0.1 --port 8000
