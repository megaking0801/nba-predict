"""Phase 0 safety net: export the legacy games table's captured market lines
(which cannot be re-fetched for free) to CSV before any destructive cutover.

Run: python -m jobs.export_legacy_csv [--out games_legacy_backup.csv]
"""
from __future__ import annotations

import argparse
import csv
import sys

from jobs.db_utils import db_connect

EXPORT_SQL = """
SELECT game_id, game_date_us, season, away_abbr, home_abbr,
       home_spread, home_odds, away_odds,
       open_home_spread, open_home_odds, open_away_odds,
       line_source, home_score, away_score, margin, cover
FROM public.games
ORDER BY game_date_us, game_id
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="games_legacy_backup.csv")
    args = ap.parse_args()

    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(EXPORT_SQL)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
    finally:
        conn.close()

    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)

    with_lines = sum(1 for r in rows if r[cols.index("home_spread")] is not None)
    print(f"[OK] exported {len(rows)} rows ({with_lines} with captured lines) "
          f"to {args.out}", flush=True)
    if not rows:
        sys.exit(1)


if __name__ == "__main__":
    main()
