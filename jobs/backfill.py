"""One-off historical backfill via leaguegamelog. Fully idempotent — resume by
re-running. ~6 requests per season.

Run: python -m jobs.backfill --seasons 2023-24,2024-25,2025-26
"""
from __future__ import annotations

import argparse
import os
import sys

from jobs.db_utils import db_connect
from jobs.ingest_stats import ingest_season, parse_season_types
from jobs.nba_http import CircuitBreaker, make_http_session
from jobs.schema import ensure_schema


def audit_season(conn, season: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT season_type, count(*) FROM public.games_v2
            WHERE season = %s GROUP BY season_type ORDER BY season_type
            """,
            (season,),
        )
        for st, n in cur.fetchall():
            print(f"[AUDIT] {season} {st}: {n} games", flush=True)
        cur.execute(
            """
            SELECT count(*) FROM public.games_v2 g
            JOIN public.team_game_stats h ON h.game_id = g.game_id AND h.is_home
            JOIN public.team_game_stats a ON a.game_id = g.game_id AND NOT a.is_home
            WHERE g.season = %s AND g.margin IS DISTINCT FROM (h.pts - a.pts)
            """,
            (season,),
        )
        bad = cur.fetchone()[0]
        if bad:
            print(f"[ERROR] {season}: {bad} games with margin/boxscore mismatch", flush=True)
        else:
            print(f"[AUDIT] {season}: margin vs boxscore consistent", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default=os.environ.get("BACKFILL_SEASONS")
                    or "2023-24,2024-25,2025-26")
    ap.add_argument("--season-types", default="regular,playin,playoffs")
    args = ap.parse_args()

    seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]
    season_types = parse_season_types(args.season_types)

    conn = db_connect()
    session = make_http_session()
    cb = CircuitBreaker(fail_threshold=int(os.environ.get("NBA_STATS_CB_THRESHOLD") or "6"))
    failed = False
    try:
        ensure_schema(conn)
        for season in seasons:
            print(f"[INFO] backfilling {season} ...", flush=True)
            results = ingest_season(conn, session, cb, season, season_types)
            if results.get("regular", (0,))[0] == 0:
                print(f"[ERROR] {season}: regular season returned 0 games", flush=True)
                failed = True
            audit_season(conn, season)
            if not cb.allow():
                print("[ERROR] circuit breaker opened; aborting (rerun resumes safely)", flush=True)
                failed = True
                break
    finally:
        conn.close()

    if failed:
        sys.exit(1)
    print("[OK] backfill complete", flush=True)


if __name__ == "__main__":
    main()
