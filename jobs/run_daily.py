"""Daily orchestrator (replaces preload_nba_data.py in the rebuilt pipeline).

Every step runs even if an earlier one fails — line/injury snapshots are
append-only history that must accumulate even on a day stats.nba.com is down.
Exit code is non-zero if anything failed so the Actions run shows red.

Run: python -m jobs.run_daily
"""
from __future__ import annotations

import os
import sys
import traceback


def _step_ingest_games():
    from jobs import ingest_games
    ingest_games.main()


def _step_ingest_stats():
    from jobs.db_utils import db_connect
    from jobs.ingest_stats import ingest_season, parse_season_types
    from jobs.nba_http import CircuitBreaker, make_http_session
    from jobs.schema import ensure_schema

    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()
    season_types = parse_season_types(
        os.environ.get("DAILY_SEASON_TYPES") or "regular,playin,playoffs")
    conn = db_connect()
    cb = CircuitBreaker()
    try:
        ensure_schema(conn)
        ingest_season(conn, make_http_session(), cb, season, season_types)
    finally:
        conn.close()
    if not cb.allow():
        raise RuntimeError("circuit breaker opened during daily stats ingest")


def _step_settle():
    from jobs import settle
    settle.main()


def _step_injuries():
    from jobs import snapshot_injuries
    snapshot_injuries.main()


def _step_lines():
    from jobs import snapshot_lines
    snapshot_lines.main()


def _step_predict():
    from jobs import predict_daily
    predict_daily.main()


STEPS = [
    ("ingest_games", _step_ingest_games),
    ("ingest_stats", _step_ingest_stats),
    ("settle", _step_settle),
    ("snapshot_injuries", _step_injuries),
    ("snapshot_lines", _step_lines),
    ("predict_daily", _step_predict),
]


def main() -> None:
    failures = []
    for name, fn in STEPS:
        print(f"===== [run_daily] {name} =====", flush=True)
        try:
            fn()
        except Exception:
            failures.append(name)
            traceback.print_exc()
            print(f"[ERROR] step {name} failed; continuing", flush=True)

    if failures:
        print(f"[ERROR] run_daily finished with failures: {failures}", flush=True)
        sys.exit(1)
    print("[OK] run_daily complete", flush=True)


if __name__ == "__main__":
    main()
