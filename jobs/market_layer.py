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
from jobs.feature_ablation import (MIN_GAIN, SEASON_SHARE, _prepare_session, block_bootstrap_ci, compare,
                                   paired_delta, per_season_delta)
from jobs.features import FEATURE_NAMES, build_feature_table, load_bundles
from jobs.evaluate import walk_forward
from jobs.model import MODEL_NAME
from webui.build_site import connect_readonly, make_run

Run = Callable[[str, tuple], pd.DataFrame]

LIVE_START = "2026-10-24"          # first day of real-timestamp line snapshots (P1)
SEASON_TYPES = ("regular", "playoffs", "playin")
Z99, Z80 = 2.58, 0.84              # 99% two-sided and 80% power

# P5.1 second layer (see the design, sections 2.3, 3, 4, 5). Everything below is fixed in advance.
TUNE_SEGMENT = {"first_season": "2015-16", "last_season": "2021-22"}
VALID_SEGMENT = {"first_season": "2022-23", "last_season": "2024-25"}
ALPHAS = tuple(CONFIG.RIDGE_ALPHA_GRID) + (300.0, 1000.0)      # 7 ridge penalties
LAMBDAS = (0.5, 1.0)                                           # shrink of the learned residual
PRIOR_ALPHA = 100.0
PRIOR_FEATURES = ["m", "absm", "elo_diff", "net_rtg30_diff", "rest_diff", "home_b2b", "away_b2b"]
FULL_FEATURES = list(FEATURE_NAMES) + ["m", "absm"]
VALID_LEVEL = 0.995                # two looks at the validation segment (ridge chosen on tune)
Z_VALID = 2.807                    # two-sided z for VALID_LEVEL

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


# ----- P5.1: second layer on the residual margin - line -----

def residual_table(features: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
    """Feature rows joined to the closing line: m = -spread (the market's expected margin) and
    resid = margin - m, the thing the second layer learns."""
    t = features.merge(lines[["game_id", "home_spread"]].drop_duplicates("game_id"), on="game_id")
    t["m"] = -t["home_spread"].astype(float)
    t["absm"] = t["m"].abs()
    t["resid"] = t["margin"].astype(float) - t["m"]
    return t.sort_values(["date", "game_id"]).reset_index(drop=True)


def residual_predictions(table: pd.DataFrame, features: list, alpha: float, lam: float = 1.0,
                         shuffle_seed: Optional[int] = None, shift: bool = False) -> pd.DataFrame:
    """Walk-forward ridge on the residual; the prediction is m + lam * r_hat, scored against the true
    margin. shuffle_seed: negative control, permute the training target within each season.
    shift: negative control, feed each game the NEXT game's features."""
    t = table.copy()
    if shift:
        t[features] = t[features].shift(-1)
        t = t.iloc[:-1]
    if shuffle_seed is not None:
        rng = np.random.default_rng(shuffle_seed)
        t["resid"] = t.groupby("season")["resid"].transform(lambda s: pd.Series(rng.permutation(s.to_numpy()), index=s.index))
    preds = walk_forward(t, "ridge", cfg=CONFIG, ridge_alpha=alpha, features=features, target="resid")
    if preds.empty:
        return preds
    keep = t[["game_id", "margin", "m"]]
    out = preds[["game_id", "date", "season", "mu", "sigma"]].rename(columns={"mu": "r_hat"}).merge(keep, on="game_id")
    out["mu"] = out["m"] + lam * out["r_hat"]
    return out[["game_id", "date", "season", "margin", "mu", "sigma", "r_hat", "m"]]


def decide_market(summary: dict) -> dict:
    """Keep rule for the second layer (design section 4). A missing piece counts as failing."""
    failed = []
    if not summary.get("mean", 1.0) <= -MIN_GAIN:
        failed.append("gain")
    if not summary.get("hi", 1.0) < 0:
        failed.append("interval")
    info = summary.get("info_over_market_candidate") or {}
    beta, se = info.get("beta"), info.get("se")
    if beta is None or se is None or not (beta - Z_VALID * se) > 0:
        failed.append("beta")
    cover = summary.get("cover_brier_delta")
    if cover is None or not cover <= 0:
        failed.append("cover_brier")
    if not summary.get("share_negative", 0.0) >= SEASON_SHARE - 1e-9:
        failed.append("seasons")
    nb, nc = summary.get("n_base_in_segment"), summary.get("n_cand_in_segment")
    if nb is not None and nc is not None and nc < nb:
        failed.append("coverage")
    return {"keep": not failed, "failed": failed}


def judge_vs_line(cand: pd.DataFrame, lines: pd.DataFrame, meta: pd.DataFrame, segment: dict,
                  reps: int = 1000, seed: int = 0, level: float = 0.99) -> dict:
    """Candidate vs the line on the same games of one segment."""
    if cand.empty:
        return {"n": 0, "keep": False, "failed": ["no_predictions"]}
    base = line_frame(lines)
    base["sigma"] = 1.0
    base = base[pd.to_datetime(base["date"]) >= pd.to_datetime(cand["date"]).min()]      # rows the walk-forward could predict
    price_lines = lines[["game_id", "home_spread", "home_price", "away_price"]]     # cover_metrics merges on game_id only
    out = compare(base, cand[["game_id", "date", "season", "margin", "mu", "sigma"]], price_lines, meta,
                  season_types=SEASON_TYPES, first_season=segment["first_season"],
                  last_season=segment["last_season"], reps=reps, seed=seed, level=level)
    if out.get("n"):
        out["market_judgement"] = decide_market(out)
    return out


def run_residual(bundles: list, lines: pd.DataFrame, reps: int = 1000, seed: int = 0,
                 n_controls: int = 0, force_full: bool = False) -> dict:
    """Prior check, tuning on the tune segment, one look at the validation segment, optional controls."""
    features_df = build_feature_table(bundles, CONFIG)
    table = residual_table(features_df, lines)
    meta = table[["game_id", "season", "season_type"]]
    out = {"n_rows": int(len(table)), "tune_segment": TUNE_SEGMENT, "validation_segment": VALID_SEGMENT}

    prior = residual_predictions(table, PRIOR_FEATURES, PRIOR_ALPHA)
    pj = judge_vs_line(prior, lines, meta, TUNE_SEGMENT, reps=reps, seed=seed)
    beta = ((pj.get("info_over_market_candidate") or {}).get("beta"))
    out["prior_check"] = {"features": PRIOR_FEATURES, "alpha": PRIOR_ALPHA, "tune": pj}
    if (beta is None or beta <= 0) and not force_full:
        out["stopped"] = "prior check: the simplest second layer has no positive beta on the tune segment; nothing was expanded"
        return out

    grid, by_alpha = [], {}
    for alpha in ALPHAS:
        preds = residual_predictions(table, FULL_FEATURES, alpha)
        by_alpha[alpha] = preds
        for lam in LAMBDAS:
            cand = preds.assign(mu=preds["m"] + lam * preds["r_hat"])
            j = judge_vs_line(cand, lines, meta, TUNE_SEGMENT, reps=200, seed=seed)
            grid.append({"alpha": alpha, "lam": lam, "mean": j.get("mean"), "n": j.get("n")})
    valid_grid = [g for g in grid if g["mean"] is not None]
    out["tune_grid"] = grid
    if not valid_grid:
        out["stopped"] = "no tune-grid result"
        return out
    best = min(valid_grid, key=lambda g: g["mean"])
    out["chosen"] = best
    preds = by_alpha[best["alpha"]]
    cand = preds.assign(mu=preds["m"] + best["lam"] * preds["r_hat"])
    out["validation"] = judge_vs_line(cand, lines, meta, VALID_SEGMENT, reps=reps, seed=seed, level=VALID_LEVEL)

    # Controls are only worth their cost when the validation segment says keep.
    if n_controls and (out["validation"].get("market_judgement") or {}).get("keep"):
        shuffled = []
        for i in range(n_controls):
            sp = residual_predictions(table, FULL_FEATURES, best["alpha"], best["lam"], shuffle_seed=1000 + i)
            shuffled.append(judge_vs_line(sp, lines, meta, VALID_SEGMENT, reps=100, seed=seed).get("mean"))
        shifted = judge_vs_line(residual_predictions(table, FULL_FEATURES, best["alpha"], best["lam"], shift=True),
                                lines, meta, VALID_SEGMENT, reps=100, seed=seed).get("mean")
        true_mean = out["validation"]["mean"]
        shuffled = [x for x in shuffled if x is not None]
        out["controls"] = {"shuffle_means": shuffled, "shift_mean": shifted,
                           "true_beats_every_shuffle": bool(shuffled) and true_mean < min(shuffled),
                           "shift_does_not_help": shifted is None or shifted >= true_mean}
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
    ap.add_argument("--report", required=True, choices=("baseline", "clv", "residual"))
    ap.add_argument("--out", help="write the result JSON here")
    ap.add_argument("--reps", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--controls", type=int, default=0, help="residual: number of shuffle controls")
    ap.add_argument("--force-full", action="store_true", help="residual: run past a failed prior check")
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
        elif args.report == "residual":
            result = run_residual(load_bundles(conn), run(_LINES, (list(SEASON_TYPES),)), reps=args.reps,
                                  seed=args.seed, n_controls=args.controls, force_full=args.force_full)
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
