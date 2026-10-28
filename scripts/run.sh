#!/usr/bin/env bash
# Run a repo module with DB secrets loaded: scripts/run.sh jobs.playbook totals
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/run_with_db.py "$@"
