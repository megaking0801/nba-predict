"""Stat-universe search over the pre-registered feature families (read-only).

    python -m jobs.family_search [--out docs/handoff/family-search.json] [--shuffles 200]

Target: the residual against the closing line (margin target: margin - (-home_spread); total target:
total - total line). Every family variant is one hypothesis, scored against THE LINE on the explore
segment; Benjamini-Hochberg (q = 0.10, m = declared search space) picks candidates, each candidate is
looked at once on the validation segment with Bonferroni; the confirmation segment is not used here.
A noise family runs alongside; if it is ever picked, the whole round is void.
See docs/superpowers/specs/2026-10-29-stat-universe-design.md.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import NormalDist
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from jobs import families as fam
from jobs.config import CONFIG
from jobs.evaluate import walk_forward
from jobs.feature_ablation import _block_ids, _prepare_session, _scrub, info_over_market, paired_delta
from jobs.features import FEATURE_NAMES, TOTAL_FEATURE_NAMES, build_feature_table, load_bundles

EXPLORE = ("2015-16", "2021-22")
VALIDATE = ("2022-23", "2024-25")
SEASON_TYPES = ("regular", "playoffs", "playin")
ALPHA_RIDGE = 100.0               # fixed in advance; no tuning, so no extra hypotheses
FDR_Q = 0.10
ALPHA = 0.01
MIN_GAIN = 0.02
SEASON_SHARE = 2 / 3
HALF_WIDTH_MAX = 0.02

_LINES = """/* fs:LINES */
SELECT g.game_id, l.home_spread::float AS home_spread, l.total_line::float AS total_line
  FROM public.games_v2 g JOIN public.v_closing_lines l ON l.game_id = g.game_id
 WHERE g.status = 'final' AND g.margin IS NOT NULL AND g.season_type = ANY(%s)
"""
_BUCKETS = """/* fs:BUCKETS */
SELECT game_id, team_abbr, pts_value, dist_bucket, corner, fga FROM public.team_shot_buckets
"""
_OFFICIALS = """/* fs:OFFICIALS */
SELECT game_id, official_name FROM public.game_officials
"""


# ----- statistics -----

def block_draws(d: pd.DataFrame, reps: int, seed: int) -> np.ndarray:
    """Bootstrap means of d['d'] resampling 14-day blocks."""
    ids = _block_ids(d)
    sums = np.bincount(ids, weights=d["d"].to_numpy(), minlength=int(ids.max()) + 1)
    counts = np.bincount(ids, minlength=int(ids.max()) + 1).astype(float)
    keep = counts > 0
    sums, counts = sums[keep], counts[keep]
    rng = np.random.default_rng(seed)
    out = np.empty(reps)
    for i in range(reps):
        pick = rng.integers(0, len(sums), len(sums))
        out[i] = sums[pick].sum() / counts[pick].sum()
    return out


def summarize(d: pd.DataFrame, level: float, reps: int, seed: int, m_tests: int) -> dict:
    """Paired |error| difference (candidate - line): mean, interval, one-sided p (H1: mean < 0) and the
    minimum detectable effect (two-sided at this level, with 50% and 80% power), plus the 50%-power MDE at
    the Bonferroni level for m_tests (the declared search space on explore, the candidate count on validate)."""
    draws = block_draws(d, reps, seed)
    mean = float(d["d"].mean())
    se = float(draws.std(ddof=1))
    a = (1 - level) / 2
    lo, hi = float(np.quantile(draws, a)), float(np.quantile(draws, 1 - a))
    z = NormalDist()
    p = float(z.cdf(mean / se)) if se > 0 else (0.0 if mean < 0 else 1.0)
    by = d.groupby("season")["d"].mean()
    return {"n": int(len(d)), "mean": mean, "lo": lo, "hi": hi, "se": se, "p_one_sided": p,
            "half_width": (hi - lo) / 2, "detectable": (hi - lo) / 2 <= HALF_WIDTH_MAX,
            "mde_50pct_power": z.inv_cdf(1 - a) * se,
            "mde_80pct_power": (z.inv_cdf(1 - a) + z.inv_cdf(0.8)) * se,
            "mde_bonferroni_m_tests": z.inv_cdf(1 - ALPHA / m_tests / 2) * se, "m_tests": m_tests,
            "per_season": {str(k): float(v) for k, v in by.items()},
            "share_seasons_negative": float((by < 0).mean())}


def benjamini_hochberg(pvals: Dict[str, float], q: float, m: int) -> List[str]:
    """Ids rejected by BH at level q with m tests (untested registered hypotheses count as p = 1)."""
    ranked = sorted(pvals.items(), key=lambda kv: kv[1])
    cut = 0
    for i, (_, p) in enumerate(ranked, start=1):
        if p <= q * i / m:
            cut = i
    return [k for k, _ in ranked[:cut]]


# ----- tables -----

def target_table(df: pd.DataFrame, lines: pd.DataFrame, target: str) -> pd.DataFrame:
    """Rows with a usable line and the residual target; 'line_mu' is the line's own prediction."""
    t = df.merge(lines, on="game_id")
    if target == "margin":
        t = t[t["home_spread"].notna()].copy()
        t["actual"] = t["margin"].astype(float)
        t["line_mu"] = -t["home_spread"].astype(float)
        t["m"], t["absm"] = t["line_mu"], t["line_mu"].abs()
        t["spread_for_beta"] = t["home_spread"].astype(float)
    else:
        t = t[t["total_line"].between(170, 280) & t["total"].notna()].copy()
        t["actual"] = t["total"].astype(float)
        t["line_mu"] = t["total_line"].astype(float)
        t["spread_for_beta"] = -t["line_mu"]           # makes info_over_market use (total - line)
    t["resid"] = t["actual"] - t["line_mu"]
    return t.sort_values(["date", "game_id"]).reset_index(drop=True)


def base_features(target: str) -> List[str]:
    return list(FEATURE_NAMES) + ["m", "absm"] if target == "margin" else list(TOTAL_FEATURE_NAMES) + ["total_line"]


def predictions(t: pd.DataFrame, features: List[str]) -> pd.DataFrame:
    p = walk_forward(t, "ridge", cfg=CONFIG, ridge_alpha=ALPHA_RIDGE, features=features, target="resid")
    if p.empty:
        return p
    out = p[["game_id", "date", "season", "mu"]].rename(columns={"mu": "r_hat"}).merge(
        t[["game_id", "actual", "line_mu", "spread_for_beta"]], on="game_id")
    out["mu"] = out["line_mu"] + out["r_hat"]
    out["margin"] = out["actual"]
    return out


def evaluate(cand: pd.DataFrame, meta: pd.DataFrame, seasons: tuple, level: float, reps: int,
             seed: int, m_tests: int) -> dict:
    ids = set(meta.loc[meta["season"].between(*seasons) & meta["season_type"].isin(SEASON_TYPES), "game_id"])
    c = cand[cand["game_id"].isin(ids)]
    if len(c) < 200:
        return {"n": int(len(c)), "too_small": True}
    line = c[["game_id", "date", "season", "margin"]].assign(mu=c["line_mu"])
    d = paired_delta(line, c[["game_id", "date", "season", "margin", "mu"]])
    out = summarize(d, level, reps, seed, m_tests)
    beta = info_over_market(c.assign(home_spread=c["spread_for_beta"]))
    z = NormalDist().inv_cdf(1 - (1 - level) / 2)
    out["beta"], out["beta_se"] = beta.get("beta"), beta.get("se")
    out["beta_lower"] = (beta["beta"] - z * beta["se"]) if beta.get("se") else None
    return out


# ----- the search -----

def run(bundles: list, lines: pd.DataFrame, extras: Dict[str, object], reps: int = 2000, seed: int = 0,
        n_shuffles: int = 200, registry: Optional[list] = None) -> dict:
    registry = registry or fam.REGISTRY
    m_tests = fam.SEARCH_SPACE
    df = build_feature_table(bundles, CONFIG)
    meta = df[["game_id", "season", "season_type"]]
    tables = {t: target_table(df, lines, t) for t in ("margin", "total")}
    out = {"search_space_declared": m_tests, "explore": EXPLORE, "validate": VALIDATE, "fdr_q": FDR_Q,
           "ridge_alpha": ALPHA_RIDGE, "not_run": fam.REGISTERED_NOT_RUN,
           "cumulative_steps": "not implemented in this round; the declared search space still counts them "
                               "(9 of 36), so the BH threshold is conservative",
           # the base features alone against the line: the reference every family is added to
           "base_reference": {t: evaluate(predictions(tables[t], base_features(t)), meta, EXPLORE, 1 - ALPHA,
                                          reps, seed, m_tests) for t in ("margin", "total")},
           "hypotheses": {}}
    built: Dict[str, pd.DataFrame] = {}
    for entry in registry:
        for vi, variant in enumerate(entry["variants"]):
            hid = f'{entry["family"]}/v{vi + 1}'
            kwargs = {k: extras.get(k) for k in entry.get("needs", [])}
            fdf = entry["build"](bundles, variant, **kwargs)
            cols = [c for c in fdf.columns if c != "game_id"]
            t = tables[entry["target"]].merge(fdf, on="game_id", how="left").fillna({c: 0.0 for c in cols})
            cand = predictions(t, base_features(entry["target"]) + cols)
            built[hid] = (entry, t, cols, cand)
            res = evaluate(cand, meta, EXPLORE, 1 - ALPHA, reps, seed, m_tests)
            res.update({"family": entry["family"], "variant": variant, "target": entry["target"]})
            if entry.get("caveat"):
                res["caveat"] = entry["caveat"]
            out["hypotheses"][hid] = {"explore": res}
    pvals = {h: v["explore"].get("p_one_sided", 1.0) for h, v in out["hypotheses"].items()
             if not v["explore"].get("too_small")}
    picked = benjamini_hochberg(pvals, FDR_Q, m_tests)
    candidates = [h for h in picked if out["hypotheses"][h]["explore"]["mean"] <= -MIN_GAIN]
    out["bh_picked"], out["candidates"] = picked, candidates
    out["voided"] = any(h.startswith("N_noise") for h in picked)
    if out["voided"]:
        out["void_reason"] = "the noise family was picked by BH: the false-positive rate is higher than assumed"
        return out
    if candidates:
        k = len(candidates)
        vlevel = 1 - ALPHA / k
        # enough draws that the Bonferroni tail quantile rests on about 20 draws
        vreps = max(reps, math.ceil(20 / (ALPHA / k / 2)))
        out["validate_level"], out["validate_reps"] = vlevel, vreps
        for h in candidates:
            entry, t, cols, cand = built[h]
            v = evaluate(cand, meta, VALIDATE, vlevel, vreps, seed, k)
            v["passes"] = bool(not v.get("too_small") and v["hi"] < 0 and v["mean"] <= -MIN_GAIN
                               and (v.get("beta_lower") or -1) > 0 and v["share_seasons_negative"] >= SEASON_SHARE - 1e-9)
            if v["passes"] and n_shuffles:
                v["controls"] = controls(t, cols, entry["target"], meta, v["mean"], n_shuffles, seed)
                v["passes"] = v["controls"]["true_beats_every_shuffle"] and v["controls"]["shift_does_not_help"]
            out["hypotheses"][h]["validate"] = v
    out["passed_validation"] = [h for h, v in out["hypotheses"].items() if v.get("validate", {}).get("passes")]
    return out


def shift_by_team(t: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    """Each game gets the family columns of its home team's NEXT game (home or away); games whose home team
    has no later game are dropped. A real signal should not survive being moved one game into the future."""
    order = t.sort_values(["date", "game_id"]).reset_index(drop=True)
    nxt, last_pos = {}, {}
    for pos in range(len(order) - 1, -1, -1):
        r = order.iloc[pos]
        nxt[pos] = last_pos.get(r["home_abbr"])
        last_pos[r["home_abbr"]] = last_pos[r["away_abbr"]] = pos
    keep = [pos for pos in range(len(order)) if nxt[pos] is not None]
    out = order.iloc[keep].copy()
    out[cols] = order.iloc[[nxt[pos] for pos in keep]][cols].to_numpy()
    return out.reset_index(drop=True)


def controls(t: pd.DataFrame, cols: List[str], target: str, meta: pd.DataFrame, true_mean: float,
             n_shuffles: int, seed: int) -> dict:
    """Shuffle the residual within season (n times) and shift the family columns to each home team's next game."""
    feats = base_features(target) + cols
    means = []
    for i in range(n_shuffles):
        rng = np.random.default_rng(seed + 1000 + i)
        s = t.copy()
        s["resid"] = s.groupby("season")["resid"].transform(lambda x: pd.Series(rng.permutation(x.to_numpy()), index=x.index))
        c = predictions(s, feats)
        c["mu"] = c["line_mu"] + c["r_hat"]                       # scored against the true result
        means.append(evaluate(c, meta, VALIDATE, 0.99, 100, seed, 1).get("mean"))
    shift_mean = evaluate(predictions(shift_by_team(t, cols), feats), meta, VALIDATE, 0.99, 100, seed, 1).get("mean")
    means = [m for m in means if m is not None]
    return {"shuffle_means": means, "shift_mean": shift_mean,
            "true_beats_every_shuffle": bool(means) and true_mean < min(means),
            "shift_does_not_help": shift_mean is None or shift_mean >= true_mean}


def main(argv: Optional[List[str]] = None) -> int:
    from webui.build_site import connect_readonly, make_run
    ap = argparse.ArgumentParser(prog="python -m jobs.family_search", description="Stat-universe family search (read-only)")
    ap.add_argument("--out")
    ap.add_argument("--reps", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--shuffles", type=int, default=200)
    args = ap.parse_args(argv)
    try:
        conn = connect_readonly()
    except Exception as exc:  # the message may carry a connection string: print the class only
        print(f"DB connection failed ({type(exc).__name__}); check the DB credentials.")
        return 2
    try:
        _prepare_session(conn)
        run_sql = make_run(conn)
        lines = run_sql(_LINES, (list(SEASON_TYPES),))
        extras = {"buckets": run_sql(_BUCKETS, ()), "officials": run_sql(_OFFICIALS, ()),
                  "total_lines": {g: t for g, t in zip(lines["game_id"], lines["total_line"])
                                  if t is not None and 170 <= t <= 280}}      # bad lines (P3 E4) excluded
        bundles = load_bundles(conn)
    except Exception as exc:
        print(f"Loading data failed: {type(exc).__name__}: {_scrub(str(exc))}")
        return 2
    finally:
        conn.close()
    result = run(bundles, lines, extras, reps=args.reps, seed=args.seed, n_shuffles=args.shuffles)
    text = json.dumps(result, indent=1, default=str, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
