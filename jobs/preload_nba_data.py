#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jobs import cache_boxscore, cache_nba, settle_daily, sync_daily


def run_step(name, fn):
    t0 = time.time()
    print(f"[STEP] start {name}", flush=True)
    fn()
    dt = time.time() - t0
    print(f"[STEP] done  {name} ({dt:.1f}s)", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Preload NBA data into DB so the Streamlit app can run in DB-only mode."
    )
    parser.add_argument("--season", default=os.environ.get("NBA_SEASON", "2025-26"))
    parser.add_argument("--cache-past-days", type=int, default=int(os.environ.get("CACHE_PAST_DAYS", "14")))
    parser.add_argument("--cache-future-days", type=int, default=int(os.environ.get("CACHE_FUTURE_DAYS", "7")))
    parser.add_argument("--backfill-past-days", type=int, default=int(os.environ.get("BACKFILL_PAST_DAYS", "30")))
    parser.add_argument("--backfill-future-days", type=int, default=int(os.environ.get("BACKFILL_FUTURE_DAYS", "2")))
    parser.add_argument("--settle-past-days", type=int, default=int(os.environ.get("SETTLE_PAST_DAYS", "180")))
    parser.add_argument("--use-odds", action="store_true", help="Enable Odds API during sync_daily.")
    parser.add_argument("--with-boxscore", action="store_true", help="Also cache player boxscore stats.")
    args = parser.parse_args()

    os.environ["NBA_SEASON"] = str(args.season)
    os.environ["CACHE_PAST_DAYS"] = str(max(1, int(args.cache_past_days)))
    os.environ["CACHE_FUTURE_DAYS"] = str(max(1, int(args.cache_future_days)))
    os.environ["BACKFILL_PAST_DAYS"] = str(max(1, int(args.backfill_past_days)))
    os.environ["BACKFILL_FUTURE_DAYS"] = str(max(1, int(args.backfill_future_days)))
    os.environ["SETTLE_PAST_DAYS"] = str(max(1, int(args.settle_past_days)))
    os.environ["USE_ODDS"] = "1" if args.use_odds else os.environ.get("USE_ODDS", "0")

    print(
        "[INFO] preload config "
        f"season={os.environ['NBA_SEASON']} "
        f"cache_window=({os.environ['CACHE_PAST_DAYS']},{os.environ['CACHE_FUTURE_DAYS']}) "
        f"backfill_window=({os.environ['BACKFILL_PAST_DAYS']},{os.environ['BACKFILL_FUTURE_DAYS']}) "
        f"settle_past_days={os.environ['SETTLE_PAST_DAYS']} "
        f"use_odds={os.environ.get('USE_ODDS', '0')} "
        f"with_boxscore={args.with_boxscore}",
        flush=True,
    )

    run_step("cache_nba", cache_nba.main)
    run_step("sync_daily", sync_daily.main)
    run_step("settle_daily", settle_daily.main)

    if args.with_boxscore:
        run_step("cache_boxscore", cache_boxscore.main)

    print("[OK] preload completed", flush=True)


if __name__ == "__main__":
    main()
