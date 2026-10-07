#!/usr/bin/env bash
# Starts the web app inside a GitHub Codespace. The database lives in data/jobs.db
# (git-ignored); on first start it is seeded from the daily data/evaluated-jobs.csv.
set -euo pipefail
cd "$(dirname "$0")/.."
python - <<'PY'
from pathlib import Path

from jobcopilot.config import Settings
from jobcopilot.db import Database

settings = Settings.from_env()
db = Database(settings.database_path)
csv = Path("data/evaluated-jobs.csv")
if csv.exists():
    print(f"Loaded {db.import_csv(csv)} jobs from {csv}")
PY
nohup python -m jobcopilot.cli serve --port 8000 > /tmp/jobcopilot.log 2>&1 &
echo "Job Copilot is starting on port 8000 (log: /tmp/jobcopilot.log)"
