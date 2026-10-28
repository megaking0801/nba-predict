"""Learning curve of the fs1 ridge (read-only): does more training data still help?

    python -m jobs.learning_curve [--out PATH] [--points 10] [--reps 2000]

The scoring games are fixed (SEGMENT, games with a closing line). For each training size N the
walk-forward is re-run with the ridge fitted on the most recent N eligible games before every fold
(N=None: expanding, as in jobs.evaluate), so the only thing that changes between points is how much
data the regression sees. Features (Elo, rolling form) are still built from the full history: this
curve measures the regression's data appetite, not the cost of having no history at all.
The Elo-only model (ridge on elo_diff) is run with the same windows as a reference, and the line's
own MAE is the horizontal bar. Intervals are block bootstraps over 14-day blocks.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np
import pandas as pd

from jobs.config import CONFIG
from jobs.evaluate import _fit_predict, cover_metrics, load_closing_lines, margin_metrics
from jobs.feature_ablation import _prepare_session, block_bootstrap_ci
from jobs.features import FEATURE_NAMES, build_feature_table, load_bundles
from jobs.model import estimate_sigma
from webui.build_site import connect_readonly

SEGMENT = {"first_season": "2022-23", "last_season": "2024-25",
           "season_types": ("regular", "playoffs", "playin")}
CI_LEVEL = 0.95
GAMES_PER_SEASON = 1320          # regular season + playoffs, only to label sizes as "about N seasons"
KINDS = ("ridge", "elo")


def size_grid(n_max: int, points: int = 10, n_min: Optional[int] = None) -> List[int]:
    """Log-spaced training sizes from the walk-forward minimum up to n_max (inclusive, rounded to 10)."""
    lo = n_min or CONFIG.WF_MIN_TRAIN_ROWS
    if n_max <= lo:
        return [int(n_max)]
    grid = np.unique((np.round(np.geomspace(lo, n_max, points) / 10) * 10).astype(int))
    grid[-1] = int(n_max)
    return [int(g) for g in np.unique(grid)]


def windowed_walk_forward(df: pd.DataFrame, kind: str, max_rows: Optional[int], cfg=CONFIG,
                          features: Sequence[str] = FEATURE_NAMES,
                          ridge_alpha: float = CONFIG.RIDGE_ALPHA) -> pd.DataFrame:
    """jobs.evaluate.walk_forward with the training set capped at the latest ``max_rows`` eligible
    games before each fold. Same folds, same sigma rule (prior folds' residuals), so max_rows=None
    reproduces walk_forward exactly. Adds ``n_train``: rows the fold actually trained on."""
    el = df[df["eligible"] & df["margin"].notna()].sort_values(["date", "game_id"]).reset_index(drop=True)
    if len(el) <= cfg.WF_MIN_TRAIN_ROWS:
        return pd.DataFrame()
    fold_start = el.iloc[cfg.WF_MIN_TRAIN_ROWS]["date"]
    last_date = el["date"].max()
    step = dt.timedelta(days=cfg.WF_STEP_DAYS)
    out, prior = [], []
    while fold_start <= last_date:
        fold_end = fold_start + step
        train = el[el["date"] < fold_start]
        test = el[(el["date"] >= fold_start) & (el["date"] < fold_end)]
        fold_start = fold_end
        if test.empty or len(train) < cfg.WF_MIN_TRAIN_ROWS:
            continue
        if max_rows is not None:
            train = train.iloc[-max_rows:]
        mu = _fit_predict(kind, train, test, ridge_alpha, cfg.HGB_MAX_ITER, list(features), "margin")
        if prior:
            sigma = estimate_sigma(prior)
        else:
            sigma = estimate_sigma(train["margin"].values - _fit_predict(
                kind, train, train, ridge_alpha, cfg.HGB_MAX_ITER, list(features), "margin"))
        for i, (_, row) in enumerate(test.iterrows()):
            out.append({"game_id": row["game_id"], "date": row["date"], "season": row["season"],
                        "season_type": row["season_type"], "margin": row["margin"],
                        "mu": float(mu[i]), "sigma": float(sigma), "n_train": int(len(train))})
        prior.extend((test["margin"].values - mu).tolist())
    return pd.DataFrame(out)


def scoring_ids(df: pd.DataFrame, lines: pd.DataFrame, seg: dict = SEGMENT) -> set:
    ok = df["season_type"].isin(seg["season_types"])
    ok &= (df["season"] >= seg["first_season"]) & (df["season"] <= seg["last_season"])
    return set(df.loc[ok, "game_id"]) & set(lines["game_id"])


def market_errors(sub: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
    """Per-game |error| of the line itself (it implies a home margin of -home_spread)."""
    m = sub[["game_id", "date", "margin"]].merge(lines[["game_id", "home_spread"]], on="game_id")
    m["market_abs"] = (m["margin"] + m["home_spread"]).abs()
    return m


def _delta_vs(a: pd.DataFrame, b_abs: pd.DataFrame, reps: int, seed: int) -> dict:
    """Mean/CI of |err_a| - |err_b| over the same games (negative: a is better)."""
    d = a.merge(b_abs[["game_id", "abs_err"]], on="game_id", suffixes=("", "_b"))
    d["d"] = (d["margin"] - d["mu"]).abs() - d["abs_err"]
    mean, lo, hi = block_bootstrap_ci(d[["date", "d"]], reps=reps, seed=seed, level=CI_LEVEL)
    return {"n": int(len(d)), "mean": mean, "lo": lo, "hi": hi}


def _abs_err(p: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"game_id": p["game_id"], "abs_err": (p["margin"] - p["mu"]).abs()})


def doubling_slope(points: List[dict], key: str = "mae", last: int = 4) -> Optional[float]:
    """Least-squares change in ``key`` per doubling of the training size, over the last ``last`` points."""
    pts = [p for p in points if p.get(key) is not None][-last:]
    if len(pts) < 3:
        return None
    x = np.log2([p["n_train_mean"] for p in pts])
    y = [p[key] for p in pts]
    return float(np.polyfit(x, y, 1)[0])


def run_curve(df: pd.DataFrame, lines: pd.DataFrame, sizes: Sequence[Optional[int]], seg: dict = SEGMENT,
              reps: int = 2000, seed: int = 0, kinds: Sequence[str] = KINDS) -> dict:
    ids = scoring_ids(df, lines, seg)
    out = {"segment": seg, "ci_level": CI_LEVEL, "sizes": [s for s in sizes],
           "note": "features built from full history; only the regression's training window varies",
           "curves": {}}
    mkt = None
    for kind in kinds:
        preds_by_size = {}
        for s in sizes:
            p = windowed_walk_forward(df, kind, s)
            preds_by_size[s] = p[p["game_id"].isin(ids)].reset_index(drop=True)
        common = set.intersection(*[set(p["game_id"]) for p in preds_by_size.values()]) if preds_by_size else set()
        if not common:
            raise ValueError("no scoring game is predicted at every training size; check the segment and the lines")
        common_df = pd.DataFrame({"game_id": sorted(common)})
        base_sub = next(iter(preds_by_size.values()))
        base_sub = base_sub[base_sub["game_id"].isin(common)]
        if mkt is None:
            mkt = market_errors(base_sub, lines)
            out["market"] = {"n": int(len(mkt)), "mae": float(mkt["market_abs"].mean())}
        market_abs = mkt.rename(columns={"market_abs": "abs_err"})[["game_id", "abs_err"]]
        full = preds_by_size[sizes[-1]]
        full = full[full["game_id"].isin(common)]
        points = []
        for s in sizes:
            p = preds_by_size[s]
            p = p[p["game_id"].isin(common)].reset_index(drop=True)
            mm = margin_metrics(p)
            cm = cover_metrics(p, lines)
            pt = {"max_rows": s, "n": int(len(p)), "n_train_mean": float(p["n_train"].mean()),
                  "approx_seasons": float(p["n_train"].mean() / GAMES_PER_SEASON),
                  "mae": mm["mae"], "rmse": mm["rmse"],
                  "cover_brier": cm["brier"] if cm else None,
                  "cover_brier_market": cm.get("brier_market") if cm else None,
                  "vs_market": _delta_vs(p, market_abs, reps, seed),
                  "vs_full": _delta_vs(p, _abs_err(full), reps, seed)}
            points.append(pt)
        out["curves"][kind] = {"points": points,
                               "mae_change_per_doubling_last4": doubling_slope(points),
                               "cover_brier_change_per_doubling_last4": doubling_slope(points, "cover_brier")}
        out["n_common_games"] = int(len(common_df))
    return out


def text_table(res: dict) -> str:
    seg = res["segment"]
    lines = [f"Learning curve, scoring {seg['first_season']}..{seg['last_season']} "
             f"({', '.join(seg['season_types'])}), games with a closing line: {res.get('n_common_games')}",
             f"Line's own MAE: {res['market']['mae']:.3f}   (95% block-bootstrap intervals)", ""]
    for kind, c in res["curves"].items():
        lines.append(f"[{kind}]  MAE change per doubling (last 4 points): "
                     f"{c['mae_change_per_doubling_last4']:+.4f}" if c["mae_change_per_doubling_last4"] is not None
                     else f"[{kind}]  (too few points for a slope)")
        lines.append(f"{'cap rows':>9} {'~seasons':>8} {'MAE':>7} {'cvBrier':>8} "
                     f"{'dMAE vs line [95% CI]':>28} {'dMAE vs full [95% CI]':>26}")
        for p in c["points"]:
            vm, vf = p["vs_market"], p["vs_full"]
            cap = "all" if p["max_rows"] is None else str(p["max_rows"])
            br = f"{p['cover_brier']:.4f}" if p["cover_brier"] is not None else "   n/a"
            lines.append(f"{cap:>9} {p['approx_seasons']:>8.1f} {p['mae']:>7.3f} {br:>8} "
                         f"{vm['mean']:>+9.3f} [{vm['lo']:+.3f},{vm['hi']:+.3f}] "
                         f"{vf['mean']:>+9.3f} [{vf['lo']:+.3f},{vf['hi']:+.3f}]")
        lines.append("")
    return "\n".join(lines)


def _scrub(text: str) -> str:
    return re.sub(r"postgres(?:ql)?://\S+", "[redacted]", text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobs.learning_curve",
                                 description="Learning curve of the fs1 ridge (read-only)")
    ap.add_argument("--out", help="write the result JSON here; a .txt table is written beside it")
    ap.add_argument("--points", type=int, default=10)
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
        bundles = load_bundles(conn)
        lines = load_closing_lines(conn)
    except Exception as exc:
        print(f"Loading data failed: {type(exc).__name__}: {_scrub(str(exc))}")
        return 2
    finally:
        conn.close()
    df = build_feature_table(bundles, CONFIG)
    ids = scoring_ids(df, lines)
    first_scored = df.loc[df["game_id"].isin(ids), "date"].min()
    # Sizes above what exists before the first scored game would all collapse to the expanding window.
    n_avail = int((df["eligible"] & df["margin"].notna() & (df["date"] < first_scored)).sum())
    sizes: List[Optional[int]] = size_grid(n_avail, args.points)[:-1] + [None]    # last point = expanding window
    res = run_curve(df, lines, sizes, reps=args.reps, seed=args.seed)
    text = text_table(res)
    if args.out:
        out = Path(args.out)
        out.write_text(json.dumps(res, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
        out.with_suffix(".txt").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
