#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from jobs.db_utils import db_connect


def main() -> None:
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.games
                    WHERE home_b2b IS NULL OR away_b2b IS NULL
                    """
                )
                before = int(cur.fetchone()[0])

                cur.execute(
                    """
                    UPDATE public.games
                    SET home_b2b = COALESCE(home_b2b, 0),
                        away_b2b = COALESCE(away_b2b, 0)
                    WHERE home_b2b IS NULL OR away_b2b IS NULL
                    """
                )
                updated = cur.rowcount

                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM public.games
                    WHERE home_b2b IS NULL OR away_b2b IS NULL
                    """
                )
                after = int(cur.fetchone()[0])

        print(f"[INFO] b2b null patch before={before} updated={updated} after={after}", flush=True)
        print("[OK] b2b null patch complete", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
