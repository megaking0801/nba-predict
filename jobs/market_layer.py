"""P5.0 market baselines (read-only): how good is the line itself, how much does fs1 miss, and how far
does the line move between prediction time and the close?

    python -m jobs.market_layer --report baseline|clv [--out PATH]

baseline: per season, the line's MAE against the result, the share of prices that are defaults, and the
          paired difference between the fs1 ridge and the line on the SAME games (how much a second
          layer would have to make up).
clv:      for predictions since the first live snapshot, the distribution of
          (closing spread - spread used at prediction time). Its standard deviation is the per-game
          spread of CLV, which fixes the sample size needed to measure it.
See docs/superpowers/specs/2026-10-26-p5-market-layer-design.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np
import pandas as pd

from jobs.config import CONFIG
from jobs.feature_ablation import (_prepare_session, block_bootstrap_ci, paired_delta, per_season_delta)
from jobs.features import FEATURE_NAMES, build_feature_table, load_bundles
from jobs.evaluate import walk_forward
from jobs.model import MODEL_NAME
from webui.build_site import connect_readonly, make_run

Run = Callable[[str, tuple], pd.DataFrame]

LIVE_START = "2026-10-24"          # first day of real-timestamp line snapshots (P1)
SEASON_TYPES = ("regular", "playoffs", "playin")
Z99, Z80 = 2.58, 0.84              # 99% two-sided and 80% power

_LINES = """/* ml:LINES */
SELECT g.game_id, g.season, g.season_type, g.game_date_et AS date, g.margin,
       l.home_spread, l.home_price, l.away_price
  FROM public.games_v2 g
  JOIN public.v_closing_lines l ON l.game_id = g.game_id
 WHERE g.status = 'final' AND g.margin IS NOT NULL AND g.season_type = ANY(%s)
   AND l.home_spread IS NOT NULL AND abs(l.home_spread) <= 25
"""

_CLV = """/* ml:CLV */
SELECT DISTINCT ON (p.game_id) p.game_id, p.predicted_at, p.home_spread_used,
       l.home_spread AS close_spread, g.tipoff_utc, g.game_date_et AS date
  FROM public.predictions p
  JOIN public.games_v2 g ON g.game_id = p.game_id
  JOIN public.v_closing_lines l ON l.game_id = p.game_id
 WHERE p.model_name = %s AND p.home_spread_used IS NOT NULL AND l.home_spread IS NOT NULL
   AND g.game_date_et >= %s AND g.tipoff_utc < now()
 ORDER BY p.game_id, p.predicted_at ASC
"""


# ----- pure summaries -----

def line_frame(lines: pd.DataFrame) -> pd.DataFrame:
    """The line as a prediction frame: the market's expected home margin is -home_spread."""
    out = lines[["game_id", "date", "season", "margin"]].copy()
    out["mu"] = -lines["home_spread"].astype(float)
    return out


def price_summary(lines: pd.DataFrame) -> pd.DataFrame:
    """Per season: games, games with both prices, and games whose two prices differ.
    Equal prices mean the default (parse_close_line falls back to 1.91 when the vendor gave none)."""
    has = lines["home_price"].notna() & lines["away_price"].notna()
    skew = has & ((lines["home_price"] - lines["away_price"]).abs() > 1e-9)
    g = pd.DataFrame({"season": lines["season"], "has": has, "skew": skew}).groupby("season")
    out = g.agg(n=("has", "size"), n_prices=("has", "sum"), n_asymmetric=("skew", "sum")).reset_index()
    out["share_asymmetric"] = out["n_asymmetric"] / out["n"]
    return out


def line_baseline(lines: pd.DataFrame) -> pd.DataFrame:
    """Per season: the line's MAE, bias (mean of margin + spread) and the push share."""
    err = lines["margin"].astype(float) + lines["home_spread"].astype(float)
    d = pd.DataFrame({"season": lines["season"], "err": err})
    g = d.groupby("season")["err"]
    out = pd.DataFrame({"n": g.size(), "mae": g.apply(lambda e: e.abs().mean()), "bias": g.mean(),
                        "push_share": g.apply(lambda e: (e.abs() < 1e-9).mean())}).reset_index()
    return out


def constant_half_brier(lines: pd.DataFrame) -> dict:
    """Brier of predicting 0.5 for every cover (pushes excluded): the benchmark when prices are defaults."""
    err = lines["margin"].astype(float) + lines["home_spread"].astype(float)
    y = (err[err.abs() > 1e-9] > 0).astype(float)
    return {"n": int(len(y)), "brier": float(((0.5 - y) ** 2).mean()), "home_cover_rate": float(y.mean())}


def gap_vs_line(ridge: pd.DataFrame, lines: pd.DataFrame, reps: int = 2000, seed: int = 0) -> dict:
    """fs1 ridge minus the line, paired on the games both have (positive = ridge is worse)."""
    base = line_frame(lines)
    d = paired_delta(base, ridge[["game_id", "date", "season", "margin", "mu"]])
    if d.empty:
        return {"n": 0}
    mean, lo, hi = block_bootstrap_ci(d, reps=reps, seed=seed)
    seasons, share_better = per_season_delta(d)
    return {"n": int(len(d)), "mean_mae_gap": mean, "lo": lo, "hi": hi, "per_season": seasons,
            "share_seasons_ridge_better": share_better}


def clv_summary(pred: pd.DataFrame, detect: float = 0.2) -> dict:
    """pred: close_spread, home_spread_used, tipoff_utc, predicted_at. Movement = close - used."""
    if pred is None or pred.empty:
        return {"n": 0}
    move = (pred["close_spread"].astype(float) - pred["home_spread_used"].astype(float)).to_numpy()
    hours = ((pd.to_datetime(pred["tipoff_utc"], utc=True) - pd.to_datetime(pred["predicted_at"], utc=True))
             .dt.total_seconds() / 3600.0).to_numpy()
    sd = float(np.std(move, ddof=1)) if len(move) > 1 else None
    out = {"n": int(len(move)), "mean_move": float(move.mean()), "sd_move": sd,
           "mean_abs_move": float(np.abs(move).mean()),
           "share_unchanged": float((np.abs(move) < 1e-9).mean()),
           "quantiles": {str(q): float(np.quantile(move, q)) for q in (0.05, 0.25, 0.5, 0.75, 0.95)},
           "mean_hours_before_tipoff": float(hours.mean())}
    if sd:
        out["picks_needed_99_80"] = float(((Z99 + Z80) * sd / detect) ** 2)
        out["picks_needed_99_half_width"] = float((Z99 * sd / detect) ** 2)
    return out


# ----- reports -----

def baseline_report(run: Run, bundles: list, reps: int = 2000, seed: int = 0) -> dict:
    lines = run(_LINES, (list(SEASON_TYPES),))
    out = {"n_games": int(len(lines)),
           "line_by_season": line_baseline(lines).to_dict("records"),
           "line_overall": {"n": int(len(lines)),
                            "mae": float((lines["margin"] + lines["home_spread"]).abs().mean())},
           "prices": price_summary(lines).to_dict("records"),
           "constant_half": constant_half_brier(lines)}
    df = build_feature_table(bundles, CONFIG)
    ridge = walk_forward(df, "ridge", cfg=CONFIG, features=FEATURE_NAMES)
    out["ridge_overall"] = {"n": int(len(ridge)), "mae": float((ridge["margin"] - ridge["mu"]).abs().mean())}
    out["ridge_minus_line"] = gap_vs_line(ridge, lines, reps=reps, seed=seed)
    return out


def clv_report(run: Run) -> dict:
    pred = run(_CLV, (MODEL_NAME, LIVE_START))
    return clv_summary(pred)


# ----- CLI -----

def _scrub(text: str) -> str:
    return re.sub(r"postgres(?:ql)?://\S+", "[redacted]", text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobs.market_layer", description="P5.0 market baselines (read-only)")
    ap.add_argument("--report", required=True, choices=("baseline", "clv"))
    ap.add_argument("--out", help="write the result JSON here")
    ap.add_argument("--reps", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    try:
        conn = connect_readonly()
    except Exception as exc:  # the message may carry a connection string: print the class only
        print(f"DB connection failed ({type(exc).__name__}); check the DB credentials.")
        return 2
    try:
        _prepare_session(conn)
        run = make_run(conn)
        if args.report == "baseline":
            result = baseline_report(run, load_bundles(conn), reps=args.reps, seed=args.seed)
        else:
            result = clv_report(run)
    except Exception as exc:
        print(f"Report failed: {type(exc).__name__}: {_scrub(str(exc))}")
        return 2
    finally:
        conn.close()
    text = json.dumps(result, indent=1, default=str, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
