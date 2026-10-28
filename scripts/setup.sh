#!/usr/bin/env bash
# One-shot setup for a fresh clone: data repo, Python 3.11 venv, dependencies, DATABASE_URL.
#
#   scripts/setup.sh              # data + env + secrets + connection check
#   scripts/setup.sh --with-tests # also run the test suite (about 3 minutes)
#
# Code (public):  https://github.com/megaking0801/nba-predict
# Data (private): https://github.com/megaking0801/nba-predict-data, cloned into data/ (gitignored here)
#
# The only secret is DATABASE_URL. It goes to .streamlit/secrets.toml (gitignored, mode 600),
# never to the repo. Re-running is safe: an existing working secrets file is kept.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$PWD
VENV="$ROOT/.venv"
SECRETS="$ROOT/.streamlit/secrets.toml"
DATA_REPO="https://github.com/megaking0801/nba-predict-data.git"

# --- 0. data repo ----------------------------------------------------------
if [ -d "$ROOT/data/.git" ]; then
  git -C "$ROOT/data" pull -q --ff-only && echo "[setup] data repo up to date"
elif git clone -q "$DATA_REPO" "$ROOT/data"; then
  echo "[setup] cloned data repo into data/"
else
  echo "[setup] could not clone $DATA_REPO (private): run 'gh auth login' first, then re-run setup"
  exit 1
fi

find_python() {
  for p in python3.13 python3.12 python3.11 python3.10; do
    if command -v "$p" >/dev/null 2>&1; then command -v "$p"; return 0; fi
  done
  return 1
}

# --- 1. venv ---------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  if PY=$(find_python); then
    echo "[setup] using $PY"
    "$PY" -m venv "$VENV"
  else
    echo "[setup] no Python >= 3.10 found; installing 3.11 with uv (in .venv-uv/)"
    python3 -m venv "$ROOT/.venv-uv"
    "$ROOT/.venv-uv/bin/pip" install -q uv
    "$ROOT/.venv-uv/bin/uv" python install 3.11
    "$ROOT/.venv-uv/bin/uv" venv --python 3.11 "$VENV"
  fi
fi
if [ -x "$ROOT/.venv-uv/bin/uv" ]; then
  "$ROOT/.venv-uv/bin/uv" pip install --python "$VENV/bin/python" -q -r requirements.txt pytest
else
  "$VENV/bin/python" -m pip install -q -r requirements.txt pytest
fi
echo "[setup] environment ready: $("$VENV/bin/python" --version)"

# --- 2. DATABASE_URL -------------------------------------------------------
check_db() {
  "$VENV/bin/python" - <<'EOF'
import sys, tomllib
import psycopg2
try:
    with open(".streamlit/secrets.toml", "rb") as f:
        url = tomllib.load(f)["DATABASE_URL"]
    psycopg2.connect(url, connect_timeout=10, sslmode="require").close()
except Exception as e:
    msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    print("[setup] DB connection FAILED:", type(e).__name__, "-", msg[:100].split("@")[0])
    sys.exit(1)
print("[setup] DB connection OK")
EOF
}

if [ -f "$SECRETS" ] && check_db; then
  echo "[setup] keeping existing $SECRETS"
else
  echo
  echo "Supabase dashboard -> Project -> Connect -> URI (Session pooler)."
  echo "Paste the URI as shown, with the [YOUR-PASSWORD] placeholder:"
  read -r URI
  printf "Database password (hidden): "
  read -r -s PW
  echo
  mkdir -p "$ROOT/.streamlit"
  # URL-encode the password and write the file without echoing either value.
  URI="$URI" PW="$PW" "$VENV/bin/python" - <<'EOF'
import os, sys
from urllib.parse import quote
uri, pw = os.environ["URI"].strip(), os.environ["PW"]
if "[YOUR-PASSWORD]" not in uri:
    sys.exit("URI must contain the [YOUR-PASSWORD] placeholder")
url = uri.replace("[YOUR-PASSWORD]", quote(pw, safe=""))
with open(".streamlit/secrets.toml", "w") as f:
    f.write('DATABASE_URL = "%s"\n' % url)
os.chmod(".streamlit/secrets.toml", 0o600)
EOF
  unset PW URI
  check_db || { echo "[setup] fix the URI or password and re-run scripts/setup.sh"; exit 1; }
fi

# --- 3. tests (optional) ---------------------------------------------------
if [ "${1:-}" = "--with-tests" ]; then
  "$VENV/bin/python" -m pytest tests -q -p no:cacheprovider
fi

echo "[setup] done. Run read-only jobs with: scripts/run.sh jobs.playbook totals"
