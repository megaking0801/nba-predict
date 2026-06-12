"""Grade unsettled predictions against final scores.

Each prediction is graded against the line it was made with
(home_spread_used), matching how a real bet would settle: home covers iff
margin + home_spread > 0; equality is a push (cover_result=2). Predictions
made without a line settle with cover_result NULL.

Run: python -m jobs.settle
"""
from __future__ import annotations

from jobs.db_utils import db_connect
from jobs.schema import ensure_schema

SETTLE_SQL = """
UPDATE public.predictions p
SET settled_at   = now(),
    cover_result = CASE
        WHEN p.home_spread_used IS NULL THEN NULL
        WHEN abs(g.margin + p.home_spread_used) < 1e-9 THEN 2
        WHEN g.margin + p.home_spread_used > 0 THEN 1
        ELSE 0
    END
FROM public.games_v2 g
WHERE g.game_id = p.game_id
  AND p.settled_at IS NULL
  AND g.status = 'final'
  AND g.margin IS NOT NULL
"""


def main() -> None:
    conn = db_connect()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(SETTLE_SQL)
            n = cur.rowcount
        conn.commit()
        print(f"[OK] settle graded {n} predictions", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
