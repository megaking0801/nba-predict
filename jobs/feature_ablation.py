"""Paired walk-forward comparison of feature/data-scope variants (read-only).

    python -m jobs.feature_ablation --experiment data_scope [--out PATH] [--with-hgb]

Every variant is scored on the SAME games (the intersection of its predictions and the fixed
scoring segment), so differences are paired per game. The keep rule and the experiments are
fixed here in advance (see docs/superpowers/specs/2026-10-26-p4-features-v1-design.md, section 6):
the confirmation segment (2025-26 onward) is never looked at by these experiments.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from jobs.config import CONFIG
from jobs.evaluate import (cover_metrics, load_closing_lines, margin_metrics,
                           split_tune_report, walk_forward)
from jobs.features import build_feature_table, load_bundles
from webui.build_site import connect_readonly

MIN_GAIN = 0.02                 # MAE points a variant must improve by to be kept
CI_LEVEL = 0.99                 # about six groups are tested, so a wider interval than 95%
SEASON_SHARE = 2 / 3            # share of seasons whose mean delta must be negative
BLOCK_DAYS = 14                 # games within two weeks are correlated: resample whole blocks
HGB_MAX_DELTA = 0.0             # an hgb check may not get worse

# Fixed scoring segment for the data-scope experiments (tune data only).
SEGMENT = {"first_season": "2019-20", "last_season": "2024-25",
           "season_types": ("regular", "playoffs", "playin")}

DATA_SCOPE_EXPERIMENTS = [
    {"name": "E1_exclude_preseason", "overrides": {"EXCLUDE_PRESEASON": True}},
    {"name": "E2a_train_from_2016-17", "overrides": {"TRAIN_FROM_SEASON": "2016-17"}},
    {"name": "E2b_train_from_2018-19", "overrides": {"TRAIN_FROM_SEASON": "2018-19"}},
]


# ----- pure statistics -----

def paired_delta(base: pd.DataFrame, cand: pd.DataFrame, target: str = "margin") -> pd.DataFrame:
    """Per-game |error| of the candidate minus |error| of the base (negative = candidate better)."""
    m = base[["game_id", "date", "season", target, "mu"]].merge(
        cand[["game_id", "mu"]], on="game_id", suffixes=("_base", "_cand"))
    m["d"] = (m[target] - m["mu_cand"]).abs() - (m[target] - m["mu_base"]).abs()
    return m[["game_id", "date", "season", "d"]].reset_index(drop=True)


def _block_ids(d: pd.DataFrame, block_days: int = BLOCK_DAYS) -> np.ndarray:
    dates = pd.to_datetime(d["date"])
    return ((dates - dates.min()).dt.days // block_days).to_numpy()


def block_bootstrap_ci(d: pd.DataFrame, reps: int = 1000, seed: int = 0,
                       level: float = CI_LEVEL, block_days: int = BLOCK_DAYS) -> Tuple[float, float, float]:
    """(mean, lo, hi) of d; resamples whole date blocks with replacement."""
    mean = float(d["d"].mean())
    ids = _block_ids(d, block_days)
    sums = np.bincount(ids, weights=d["d"].to_numpy(), minlength=int(ids.max()) + 1)
    counts = np.bincount(ids, minlength=int(ids.max()) + 1).astype(float)
    keep = counts > 0
    sums, counts = sums[keep], counts[keep]
    rng = np.random.default_rng(seed)
    draws = np.empty(reps)
    for i in range(reps):
        pick = rng.integers(0, len(sums), len(sums))
        draws[i] = sums[pick].sum() / counts[pick].sum()
    a = (1.0 - level) / 2.0
    return mean, float(np.quantile(draws, a)), float(np.quantile(draws, 1.0 - a))


def per_season_delta(d: pd.DataFrame) -> Tuple[Dict[str, float], float]:
    by = d.groupby("season")["d"].mean()
    return {str(k): float(v) for k, v in by.items()}, float((by < 0).mean())


def decide(summary: dict) -> dict:
    """The pre-registered keep rule. A missing cover Brier counts as failing."""
    failed = []
    if not summary["mean"] <= -MIN_GAIN:
        failed.append("gain")
    if not summary["hi"] < 0:
        failed.append("interval")
    if not summary["share_negative"] >= SEASON_SHARE - 1e-9:
        failed.append("seasons")
    cover = summary.get("cover_brier_delta")
    if cover is None or not cover <= 0:
        failed.append("cover_brier")
    return {"keep": not failed, "failed": failed}


def detectability(halfwidth: float) -> dict:
    """If the interval is wider than the minimum gain, a miss means 'cannot detect', not 'no effect'."""
    return {"halfwidth": halfwidth, "detectable": halfwidth <= MIN_GAIN}


def info_over_market(df: pd.DataFrame) -> dict:
    """Regress margin + home_spread on mu + home_spread (model minus the line's implied margin).
    A slope clearly above 0 means the model knows something the line does not."""
    y = (df["margin"] + df["home_spread"]).to_numpy(dtype=float)
    x = (df["mu"] + df["home_spread"]).to_numpy(dtype=float)
    n = len(y)
    xc, yc = x - x.mean(), y - y.mean()
    denom = float((xc ** 2).sum())
    if n < 3 or denom < 1e-12:
        return {"n": n, "beta": 0.0, "se": None}
    beta = float((xc * yc).sum() / denom)
    resid = yc - beta * xc
    se = float(np.sqrt((resid ** 2).sum() / (n - 2) / denom))
    return {"n": n, "beta": beta, "se": se}


# ----- one comparison -----

def _segment_ids(meta: pd.DataFrame, seasons: pd.Series, season_types, first_season, last_season) -> set:
    ok = pd.Series(True, index=meta.index)
    if season_types:
        ok &= meta["season_type"].isin(season_types)
    if "season" in meta:
        s = meta["season"]
        if first_season:
            ok &= s >= first_season
        if last_season:
            ok &= s <= last_season
    return set(meta.loc[ok, "game_id"])


def compare(base: pd.DataFrame, cand: pd.DataFrame, lines: pd.DataFrame, meta: pd.DataFrame,
            season_types=None, first_season: Optional[str] = None, last_season: Optional[str] = None,
            reps: int = 1000, seed: int = 0) -> dict:
    """Candidate vs base on the same games inside the scoring segment."""
    meta = meta.copy()
    if "season" not in meta:
        meta = meta.merge(base[["game_id", "season"]].drop_duplicates("game_id"), on="game_id", how="left")
    ids = _segment_ids(meta, None, season_types, first_season, last_season)
    b, c = base[base["game_id"].isin(ids)], cand[cand["game_id"].isin(ids)]
    d = paired_delta(b, c)
    mean, lo, hi = block_bootstrap_ci(d, reps=reps, seed=seed)
    seasons, share = per_season_delta(d)
    common = d["game_id"]
    cover_delta = None
    if lines is not None and len(lines):
        cb = cover_metrics(b[b["game_id"].isin(common)], lines)
        cc = cover_metrics(c[c["game_id"].isin(common)], lines)
        if cb and cc:
            cover_delta = float(cc["brier"] - cb["brier"])
    out = {"n": int(len(d)), "mean": mean, "lo": lo, "hi": hi, "share_negative": share,
           "per_season": seasons, "cover_brier_delta": cover_delta}
    out.update(decide(out))
    out.update(detectability((hi - lo) / 2.0))
    if lines is not None and len(lines):
        joined = c[c["game_id"].isin(common)].merge(lines[["game_id", "home_spread"]], on="game_id")
        if len(joined) >= 30:
            out["info_over_market_candidate"] = info_over_market(joined)
            jb = b[b["game_id"].isin(common)].merge(lines[["game_id", "home_spread"]], on="game_id")
            out["info_over_market_base"] = info_over_market(jb)
    return out


# ----- the data-scope experiment -----

def _variant_preds(bundles, cfg, kind: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    df = build_feature_table(bundles, cfg)
    return df, walk_forward(df, kind, cfg=cfg)


def _judge(results_by_kind: dict) -> dict:
    """Ridge must pass the rule; hgb (when run) must not get worse."""
    r = results_by_kind["ridge"]
    keep = bool(r["keep"])
    if "hgb" in results_by_kind:
        keep = keep and results_by_kind["hgb"]["mean"] <= HGB_MAX_DELTA
    return {"keep": keep}


def run_data_scope(bundles: list, lines: pd.DataFrame, reps: int = 1000, seed: int = 0,
                   kinds: Tuple[str, ...] = ("ridge",)) -> dict:
    seg = dict(SEGMENT)
    base_cfg = CONFIG
    base = {}
    meta = None
    for kind in kinds:
        df, preds = _variant_preds(bundles, base_cfg, kind)
        base[kind] = preds
        if meta is None:
            meta = df[["game_id", "season", "season_type"]]
    tune, report = split_tune_report(base["ridge"])
    out = {"segment": seg,
           "baseline_selfcheck": {"report_margin": margin_metrics(report),
                                  "report_cover": cover_metrics(report, lines) if len(lines) else None}}

    def run_variant(overrides: dict) -> dict:
        cfg = dataclasses.replace(base_cfg, **overrides)
        res = {}
        for kind in kinds:
            _, preds = _variant_preds(bundles, cfg, kind)
            res[kind] = compare(base[kind], preds, lines, meta, season_types=seg["season_types"],
                                first_season=seg["first_season"], last_season=seg["last_season"],
                                reps=reps, seed=seed)
        res["overrides"] = overrides
        res["judgement"] = _judge(res)
        return res

    out["experiments"] = {e["name"]: run_variant(e["overrides"]) for e in DATA_SCOPE_EXPERIMENTS}
    kept = {n: r for n, r in out["experiments"].items() if r["judgement"]["keep"]}
    merged: dict = {}
    e1 = kept.get("E1_exclude_preseason")
    if e1:
        merged.update(e1["overrides"])
    e2 = [r for n, r in kept.items() if n.startswith("E2")]
    if e2:
        merged.update(min(e2, key=lambda r: r["ridge"]["mean"])["overrides"])
    out["E3_combined"] = run_variant(merged) if merged else {"skipped": "no variant met the keep rule"}
    return out


# ----- CLI -----

def _scrub(text: str) -> str:
    return re.sub(r"postgres(?:ql)?://\S+", "[redacted]", text)


def _prepare_session(conn) -> None:
    """Loading 12 seasons of box scores takes longer than the 30 s default of connect_readonly."""
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 600000")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobs.feature_ablation",
                                 description="Paired walk-forward comparison (read-only)")
    ap.add_argument("--experiment", required=True, choices=("data_scope",))
    ap.add_argument("--out", help="write the result JSON here")
    ap.add_argument("--with-hgb", action="store_true", help="also run hgb as a no-worse check")
    ap.add_argument("--reps", type=int, default=1000)
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
    kinds = ("ridge", "hgb") if args.with_hgb else ("ridge",)
    result = run_data_scope(bundles, lines, reps=args.reps, seed=args.seed, kinds=kinds)
    text = json.dumps(result, indent=1, default=str, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
