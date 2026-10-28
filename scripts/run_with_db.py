"""Run any `python -m` module with DATABASE_URL loaded from .streamlit/secrets.toml.

Usage (from the repo root): scripts/run.sh jobs.playbook totals

Sets DATABASE_URL / SUPABASE_* in os.environ and never prints a value.
"""
import os
import runpy
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / ".streamlit" / "secrets.toml"
KEYS_PREFIX = ("DATABASE_URL", "SUPABASE_")


def load_secrets() -> list[str]:
    if not SECRETS.exists():
        sys.exit(f"{SECRETS} not found; run scripts/setup.sh first")
    with SECRETS.open("rb") as f:
        data = tomllib.load(f)
    loaded = []
    for key, value in data.items():
        if isinstance(value, str) and key.startswith(KEYS_PREFIX):
            os.environ[key] = value
            loaded.append(key)
    return loaded


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: run_with_db.py <module> [args...]")
    module, args = sys.argv[1], sys.argv[2:]
    loaded = load_secrets()
    print(f"[run_with_db] loaded keys: {', '.join(loaded) or 'none'}", file=sys.stderr)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    sys.argv = [module, *args]
    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
