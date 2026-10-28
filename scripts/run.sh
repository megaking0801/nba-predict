#!/usr/bin/env bash
# Run a repo module with DB secrets loaded: scripts/run.sh jobs.playbook totals
set -euo pipefail
cd "$(dirname "$0")/.."
status=0
.venv/bin/python scripts/run_with_db.py "$@" || status=$?
# Fetched data lives in the private data repo (data/); unpushed data is lost on a computer switch.
if [ -d data/.git ] && [ -n "$(git -C data status --porcelain)" ]; then
  echo "[run] data/ has uncommitted changes: cd data && git add -A && git commit -m '...' && git push" >&2
fi
exit $status
