"""Populate predictions for past games with honest out-of-time (walk-forward)
forecasts, so the public app has real 'prediction vs result' content even in
the off-season. These are NOT in-sample — each game is predicted by a model
trained only on prior games, the same truthful basis as the track record.

Run: python -m jobs.predict_history
"""
from __future__ import annotations

import psycopg2.extras

from jobs.db_utils import db_connect
from jobs.evaluate import load_closing_lines, walk_forward
from jobs.features import build_feature_table, load_bundles
from jobs.model import (MODEL_NAME, margin_to_cover_prob, margin_to_win_prob)
from jobs.schema import ensure_schema

INSERT = """
INSERT INTO public.predictions
  (game_id, model_name, model_version, pred_margin, p_raw, p_home_cover,
   p_home_win, home_spread_used)
VALUES %s
"""


def _active_version(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT model_version FROM public.model_registry_v2 "
                    "WHERE model_name=%s AND is_active", (MODEL_NAME,))
        r = cur.fetchone()
    return r[0] if r else "history"


def main() -> None:
    conn = db_connect()
    try:
        ensure_schema(conn)
        version = _active_version(conn)
        df = build_feature_table(load_bundles(conn))
        lines = load_closing_lines(conn).set_index("game_id")
        preds = walk_forward(df, "ridge", ridge_alpha=3.0)
        if preds.empty:
            print("[WARN] no walk-forward predictions produced", flush=True)
            return

        rows = []
        for _, r in preds.iterrows():
            mu, sigma = float(r["mu"]), float(r["sigma"])
            spread = float(lines.loc[r["game_id"], "home_spread"]) \
                if r["game_id"] in lines.index else None
            cover = margin_to_cover_prob(mu, sigma, spread)["p_raw"] if spread is not None else None
            rows.append((r["game_id"], MODEL_NAME, version, mu, cover, cover,
                         margin_to_win_prob(mu, sigma), spread))

        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.predictions WHERE model_name=%s", (MODEL_NAME,))
            psycopg2.extras.execute_values(cur, INSERT, rows, page_size=1000)
        conn.commit()
        print(f"[OK] wrote {len(rows)} historical predictions (version={version})", flush=True)
    finally:
        conn.close()

    # settle fills win_result + cover_result against final scores
    from jobs import settle
    settle.main()


if __name__ == "__main__":
    main()
