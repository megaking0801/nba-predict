#!/usr/bin/env python3
"""Stop hook: run the test suite before Claude finishes and block the stop if it fails.

Design notes
- Reads the hook payload from stdin. If ``stop_hook_active`` is true, Claude is already
  continuing because of an earlier block from this hook, so exit 0 (no infinite loop).
  The gate therefore blocks at most once per stop attempt.
- Only runs when there are changed or untracked ``*.py`` files (``git status``), so
  chat-only turns and pre-existing failures don't block unrelated work.
- Failure is reported with ``{"decision": "block", "reason": ...}`` on stdout, exit 0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

TIMEOUT_S = 150
TAIL_LINES = 40


def _run(cmd: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if payload.get("stop_hook_active"):
        return 0

    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    try:
        changed = _run(["git", "status", "--porcelain", "--", "*.py"], root, 30).stdout.strip()
    except Exception:
        return 0  # not a git checkout or git missing: don't gate
    if not changed:
        return 0

    try:
        res = _run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
                   root, TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(json.dumps({"decision": "block",
                          "reason": f"pytest timed out after {TIMEOUT_S}s. Investigate before finishing."}))
        return 0

    if res.returncode == 0:
        return 0

    tail = "\n".join((res.stdout + res.stderr).strip().splitlines()[-TAIL_LINES:])
    print(json.dumps({
        "decision": "block",
        "reason": ("pytest failed (exit %d). Fix the failures or report them honestly "
                   "before finishing.\n\n%s" % (res.returncode, tail)),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
