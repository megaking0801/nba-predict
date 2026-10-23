"""Daily orchestrator (replaces preload_nba_data.py in the rebuilt pipeline).

Every step runs even if an earlier one fails — line/injury snapshots are
append-only history that must accumulate even on a day one source is down.
Exit code is non-zero if anything failed so the Actions run shows red.
Data comes from ESPN only (stats.nba.com / cdn.nba.com block GitHub runners).

Run: python -m jobs.run_daily           # full daily run, makes the day's prediction
     python -m jobs.run_daily --close   # near-tipoff snapshots only
"""
from __future__ import annotations

import argparse
import sys
import traceback


def _step_ingest_espn():
    from jobs import ingest_espn
    ingest_espn.daily(days_back=3, days_ahead=2)


def _step_settle():
    from jobs import settle
    settle.main()


def _step_injuries():
    from jobs import snapshot_injuries
    snapshot_injuries.main()


def _step_rosters():
    from jobs import snapshot_rosters
    snapshot_rosters.main()


def _step_espn_odds():
    from jobs.ingest_espn import snapshot_espn_odds
    snapshot_espn_odds()


# Close snapshots only fetch games tipping off within this many minutes.
CLOSE_WINDOW_MINUTES = 150


def _step_espn_odds_close():
    from jobs.ingest_espn import snapshot_espn_odds
    snapshot_espn_odds(window_minutes=CLOSE_WINDOW_MINUTES)


def _step_lines():
    from jobs import snapshot_lines
    snapshot_lines.main()


def _step_predict():
    from jobs import predict_daily
    predict_daily.main()


# Full daily run (10:30 UTC = 18:30 Taipei): the official prediction of the day.
STEPS = [
    ("ingest_espn", _step_ingest_espn),
    ("settle", _step_settle),
    ("snapshot_injuries", _step_injuries),
    ("snapshot_rosters", _step_rosters),
    ("espn_odds", _step_espn_odds),
    ("snapshot_lines", _step_lines),
    ("predict_daily", _step_predict),
]

# Close snapshots (--close): lines and injuries near tipoff, never a prediction.
CLOSE_STEPS = [
    ("espn_odds_close", _step_espn_odds_close),
    ("snapshot_injuries", _step_injuries),
    ("snapshot_lines", _step_lines),
]


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Daily pipeline")
    parser.add_argument("--close", action="store_true",
                        help="close-snapshot mode: odds/injuries near tipoff, no prediction")
    args = parser.parse_args(argv)
    steps = CLOSE_STEPS if args.close else STEPS

    failures = []
    for name, fn in steps:
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
