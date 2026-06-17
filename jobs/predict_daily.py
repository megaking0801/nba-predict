"""Serving: replay settled games through the FeatureBuilder, emit features
for the upcoming slate, convert margin -> calibrated cover probability ->
edges -> picks, and append to predictions (+ game_features for audit).

Graceful no-op while no trained model is active (pre-backfill state).
Run: python -m jobs.predict_daily
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Dict, List, Optional, Set, Tuple

from jobs.config import CONFIG
from jobs.db_utils import db_connect
from jobs.features import load_bundles, norm_player_name, replay_and_emit
from jobs.model import CALIBRATOR_NAME, MODEL_NAME, load_active
from jobs.picks import cap_picks_per_day, decide_game, effective_min_edge
from jobs.schema import ensure_schema
from jobs.tz import et_today, utc_now


def load_upcoming(conn, days_ahead: int) -> List[dict]:
    today = et_today()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT game_id, season, game_date_et, home_abbr, away_abbr
            FROM public.games_v2
            WHERE game_date_et BETWEEN %s AND %s AND status = 'scheduled'
            ORDER BY game_date_et, game_id
            """,
            (today, today + dt.timedelta(days=days_ahead)),
        )
        return [{"game_id": r[0], "season": r[1], "date": r[2],
                 "game_date_et": r[2], "home_abbr": r[3], "away_abbr": r[4]}
                for r in cur.fetchall()]


def load_latest_lines(conn, game_ids: List[str]) -> Dict[str, dict]:
    if not game_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT game_id, line_id, home_spread, home_price, away_price, captured_at
            FROM public.v_latest_lines WHERE game_id = ANY(%s)
            """,
            (game_ids,),
        )
        return {r[0]: {"line_id": r[1], "home_spread": r[2], "home_price": r[3],
                       "away_price": r[4], "captured_at": r[5]}
                for r in cur.fetchall()}


def load_out_players(conn) -> Dict[str, Set[str]]:
    """team_abbr -> normalized names listed OUT in today's snapshot."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT team_abbr, player_name FROM public.injury_snapshots
            WHERE snapshot_date_et = %s AND status ILIKE '%%out%%'
            """,
            (et_today(),),
        )
        out: Dict[str, Set[str]] = {}
        for team, name in cur.fetchall():
            out.setdefault(team, set()).add(norm_player_name(name))
        return out


def injury_veto_for(builder, abbr: str, out_names: Set[str], cfg=CONFIG) -> bool:
    """Veto when a top-talent player who played the previous game is now OUT —
    the one situation where the realized-talent features are provably blind."""
    if not out_names:
        return False
    top = builder.team_top_talent(abbr, cfg.INJURY_VETO_TALENT_RANK)
    last_game = builder.team(abbr).last_game_players
    return any(name in out_names and name in last_game for name, _ in top)


def is_paper_mode(conn, cfg=CONFIG) -> bool:
    forced = (os.environ.get("PAPER_MODE") or "").strip()
    if forced == "1":
        return True
    if forced == "0":
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT min(predicted_at) FROM public.predictions")
        first = cur.fetchone()[0]
    if first is None:
        return True
    return utc_now() < first + dt.timedelta(weeks=cfg.PAPER_MODE_WEEKS)


def main() -> None:
    days_ahead = int(os.environ.get("PREDICT_FUTURE_DAYS") or "2")
    conn = db_connect()
    try:
        ensure_schema(conn)

        payload_wrap, _ = load_active(conn, MODEL_NAME)
        if not payload_wrap:
            print("[WARN] no active margin_model; skipping predictions", flush=True)
            return
        model = payload_wrap["model"]
        sigma = float(payload_wrap["sigma"])
        feature_names = payload_wrap["feature_names"]
        model_version = None
        with conn.cursor() as cur:
            cur.execute("SELECT model_version FROM public.model_registry_v2 "
                        "WHERE model_name = %s AND is_active", (MODEL_NAME,))
            row = cur.fetchone()
            model_version = row[0] if row else "unknown"

        cal_wrap, cal_metrics = load_active(conn, CALIBRATOR_NAME)
        calibrator = (cal_wrap or {}).get("calibrator") or {"type": "identity"}
        min_edge = effective_min_edge(cal_metrics)

        upcoming = load_upcoming(conn, days_ahead)
        if not upcoming:
            print("[INFO] no scheduled games in window; nothing to predict", flush=True)
            return

        bundles = load_bundles(conn)
        rows, builder = replay_and_emit(bundles, upcoming)
        lines = load_latest_lines(conn, [g["game_id"] for g in upcoming])
        out_players = load_out_players(conn)
        paper = is_paper_mode(conn)
        now = utc_now()

        decisions = []
        for row in rows:
            X = [[float(row[name]) for name in feature_names]]
            mu = float(model.predict(X)[0])
            line = lines.get(row["game_id"])
            age_h = None
            if line and line["captured_at"]:
                age_h = (now - line["captured_at"]).total_seconds() / 3600.0
            veto = (injury_veto_for(builder, row["home_abbr"], out_players.get(row["home_abbr"], set()))
                    or injury_veto_for(builder, row["away_abbr"], out_players.get(row["away_abbr"], set())))
            d = decide_game(
                mu, sigma,
                home_spread=line["home_spread"] if line else None,
                home_price=line["home_price"] if line else None,
                away_price=line["away_price"] if line else None,
                calibrator=calibrator, min_edge=min_edge,
                line_age_hours=age_h,
                games_played_min=row["games_played_min"],
                injury_veto=veto,
            )
            d.update({
                "game_id": row["game_id"], "game_date_et": row["game_date_et"],
                "line_id": line["line_id"] if line else None,
                "home_spread": line["home_spread"] if line else None,
                "home_price": line["home_price"] if line else None,
                "away_price": line["away_price"] if line else None,
                "features": {name: float(row[name]) for name in feature_names},
            })
            decisions.append(d)

        cap_picks_per_day(decisions)

        n_picks = 0
        with conn.cursor() as cur:
            for d in decisions:
                edge = d["edge_home"] if d["edge_home"] is not None else None
                cur.execute(
                    """
                    INSERT INTO public.predictions
                      (game_id, model_name, model_version, line_id_used,
                       home_spread_used, home_price_used, away_price_used,
                       pred_margin, p_raw, p_home_cover, p_home_win, edge_prob, ev_home,
                       pick_side, abstain_reason, is_paper)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (d["game_id"], MODEL_NAME, model_version, d["line_id"],
                     d["home_spread"], d["home_price"], d["away_price"],
                     d["pred_margin"], d["p_raw"], d["p_cal"], d["p_home_win"], edge,
                     d["ev_home"], d["pick_side"], d["abstain_reason"], paper),
                )
                cur.execute(
                    """
                    INSERT INTO public.game_features (game_id, feature_set, features, built_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (game_id, feature_set) DO UPDATE SET
                      features = EXCLUDED.features, built_at = now()
                    """,
                    (d["game_id"], CONFIG.FEATURE_SET, json.dumps(d["features"])),
                )
                if d["pick_side"]:
                    n_picks += 1
        conn.commit()
        print(f"[OK] predict_daily wrote {len(decisions)} predictions "
              f"({n_picks} picks, paper={paper}, min_edge={min_edge})", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
