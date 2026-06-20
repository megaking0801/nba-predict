"""Walk-forward evaluation harness — the permanent out-of-time measuring
stick (the old system's deepest flaw was that nothing ever measured
out-of-sample). Run standalone for a report: python -m jobs.evaluate
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from jobs.config import CONFIG
from jobs.db_utils import db_connect
from jobs.features import (FEATURE_NAMES, TOTAL_FEATURE_NAMES,
                           build_feature_table, load_bundles)
from jobs.model import (devig, estimate_sigma, fit_candidate,
                        margin_to_cover_prob, total_to_over_prob)
from jobs.picks import cap_picks_per_day, decide_game

BASELINE_KINDS = ("const", "elo")


def _fit_predict(kind: str, train: pd.DataFrame, test: pd.DataFrame,
                 ridge_alpha: float, hgb_iter: int,
                 features: List[str] = FEATURE_NAMES, target: str = "margin") -> np.ndarray:
    if kind == "const":
        return np.full(len(test), float(train[target].mean()))
    if kind == "elo":
        m = fit_candidate("ridge", train[["elo_diff"]].values, train[target].values,
                          ridge_alpha=ridge_alpha)
        return m.predict(test[["elo_diff"]].values)
    m = fit_candidate(kind, train[features].values, train[target].values,
                      ridge_alpha=ridge_alpha, hgb_iter=hgb_iter)
    return m.predict(test[features].values)


def walk_forward(df: pd.DataFrame, kind: str, cfg=CONFIG,
                 ridge_alpha: float = CONFIG.RIDGE_ALPHA,
                 hgb_iter: int = CONFIG.HGB_MAX_ITER,
                 features: List[str] = FEATURE_NAMES, target: str = "margin") -> pd.DataFrame:
    """Expanding-window walk-forward. Returns out-of-time predictions with a
    per-row sigma estimated only from residuals of PRIOR folds. `target`/`features`
    default to the margin model; pass total + TOTAL_FEATURE_NAMES for the totals model."""
    el = df[df["eligible"] & df[target].notna()].sort_values(["date", "game_id"]).reset_index(drop=True)
    if len(el) <= cfg.WF_MIN_TRAIN_ROWS:
        return pd.DataFrame()

    fold_start = el.iloc[cfg.WF_MIN_TRAIN_ROWS]["date"]
    last_date = el["date"].max()
    step = dt.timedelta(days=cfg.WF_STEP_DAYS)

    out_rows: List[dict] = []
    prior_residuals: List[float] = []
    while fold_start <= last_date:
        fold_end = fold_start + step
        train = el[el["date"] < fold_start]
        test = el[(el["date"] >= fold_start) & (el["date"] < fold_end)]
        fold_start = fold_end
        if test.empty or len(train) < cfg.WF_MIN_TRAIN_ROWS:
            continue

        mu = _fit_predict(kind, train, test, ridge_alpha, hgb_iter, features, target)
        if prior_residuals:
            sigma = estimate_sigma(prior_residuals)
        else:  # first fold only: in-sample train residual spread (no peeking at test)
            mu_tr = _fit_predict(kind, train, train, ridge_alpha, hgb_iter, features, target)
            sigma = estimate_sigma(train[target].values - mu_tr)

        res = test[target].values - mu
        for i, (_, row) in enumerate(test.iterrows()):
            out_rows.append({
                "game_id": row["game_id"], "date": row["date"],
                "season": row["season"], target: row[target],
                "mu": float(mu[i]), "sigma": float(sigma),
            })
        prior_residuals.extend(res.tolist())

    return pd.DataFrame(out_rows)


def margin_metrics(preds: pd.DataFrame) -> Optional[dict]:
    if preds.empty:
        return None
    err = preds["margin"] - preds["mu"]
    return {
        "n": int(len(preds)),
        "mae": float(err.abs().mean()),
        "rmse": float(np.sqrt((err ** 2).mean())),
        "bias": float(err.mean()),
        "resid_sigma": float(err.std(ddof=1)),
    }


def load_closing_lines(conn) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT l.game_id, l.home_spread, l.home_price, l.away_price
            FROM public.v_closing_lines l
            JOIN public.games_v2 g ON g.game_id = l.game_id
            WHERE g.status = 'final' AND g.margin IS NOT NULL
        """)
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=["game_id", "home_spread", "home_price", "away_price"])


def cover_metrics(preds: pd.DataFrame, lines: pd.DataFrame) -> Optional[dict]:
    """Brier/log-loss/calibration of derived cover probs on lined games."""
    if preds.empty or lines.empty:
        return None
    df = preds.merge(lines, on="game_id", how="inner")
    if df.empty:
        return None

    records = []
    for _, r in df.iterrows():
        pr = margin_to_cover_prob(r["mu"], r["sigma"], r["home_spread"])
        actual = r["margin"] + r["home_spread"]
        if abs(actual) < 1e-9:
            continue  # push: refunded, excluded (probs are push-conditional)
        y = 1.0 if actual > 0 else 0.0
        rec = {"p": pr["p_raw"], "y": y}
        if r["home_price"] and r["away_price"]:
            rec["p_market"] = devig(r["home_price"], r["away_price"])[0]
        records.append(rec)
    if len(records) < 20:
        return None

    e = pd.DataFrame(records)
    p, y = e["p"].values, e["y"].values
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    out = {
        "n": int(len(e)),
        "brier": float(((p - y) ** 2).mean()),
        "logloss": float(-(y * np.log(pc) + (1 - y) * np.log(1 - pc)).mean()),
        "brier_p50": 0.25,
    }
    if "p_market" in e and e["p_market"].notna().sum() >= 20:
        pm = e["p_market"].dropna()
        ym = e.loc[pm.index, "y"]
        out["brier_market"] = float(((pm - ym) ** 2).mean())

    # calibration slope/intercept: logistic fit of y on logit(p)
    try:
        from sklearn.linear_model import LogisticRegression
        lz = np.log(pc / (1 - pc)).reshape(-1, 1)
        lr = LogisticRegression(C=1e9, solver="lbfgs").fit(lz, y)
        out["cal_slope"] = float(lr.coef_[0][0])
        out["cal_intercept"] = float(lr.intercept_[0])
    except Exception:
        pass
    return out


def straight_up_accuracy(preds: pd.DataFrame) -> Optional[dict]:
    """How often sign(mu) matches sign(margin) — the moneyline 'pick the
    winner' hit-rate. NBA has no ties, so every game is graded."""
    if preds.empty:
        return None
    pred_home = preds["mu"] > 0
    actual_home = preds["margin"] > 0
    return {
        "n": int(len(preds)),
        "winner_accuracy": float((pred_home == actual_home).mean()),
    }


def _simulate_policy(preds: pd.DataFrame, lines: pd.DataFrame, min_edge: float,
                     cfg=CONFIG) -> dict:
    """Replay the real pick policy (decide_game + cap_picks_per_day) over
    walk-forward predictions joined to their lines, then settle ATS results.

    Conservative, leakage-free by construction: identity calibrator (no
    forward-fit that could peek), closing lines so no stale guard, and the
    early-season/injury guards are already handled by walk-forward eligibility
    and unavailable history respectively — so both are left off here."""
    df = preds.merge(lines, on="game_id", how="inner")
    if df.empty:
        return {"n_picks": 0}

    decisions: List[dict] = []
    for _, r in df.iterrows():
        if r["home_spread"] is None or pd.isna(r["home_spread"]):
            continue
        d = decide_game(
            r["mu"], r["sigma"],
            home_spread=float(r["home_spread"]),
            home_price=(float(r["home_price"]) if pd.notna(r["home_price"]) else None),
            away_price=(float(r["away_price"]) if pd.notna(r["away_price"]) else None),
            calibrator=None, min_edge=min_edge,
            line_age_hours=None, games_played_min=None, injury_veto=False, cfg=cfg)
        d["game_date_et"] = r["date"]
        d["_margin"] = float(r["margin"])
        d["_home_spread"] = float(r["home_spread"])
        d["_home_price"] = float(r["home_price"]) if pd.notna(r["home_price"]) else None
        d["_away_price"] = float(r["away_price"]) if pd.notna(r["away_price"]) else None
        decisions.append(d)

    cap_picks_per_day(decisions, cfg=cfg)

    n_graded = wins = pushes = 0
    pnl = 0.0
    for d in decisions:
        if not d.get("pick_side"):
            continue
        actual = d["_margin"] + d["_home_spread"]
        if abs(actual) < 1e-9:
            pushes += 1
            continue  # refunded: no stake at risk
        home_covers = actual > 0
        won = (d["pick_side"] == "HOME") == home_covers
        price = d["_home_price"] if d["pick_side"] == "HOME" else d["_away_price"]
        if not price or price <= 1.0:
            price = 1.91  # -110 fallback when the book price is missing
        pnl += (price - 1.0) if won else -1.0
        n_graded += 1
        wins += int(won)

    n_picks = sum(1 for d in decisions if d.get("pick_side"))
    return {
        "min_edge": round(float(min_edge), 4),
        "n_picks": n_picks,            # picks placed (incl. eventual pushes)
        "n_graded": n_graded,          # excludes pushes
        "pushes": pushes,
        "hit_rate": (wins / n_graded) if n_graded else None,
        "roi": (pnl / n_graded) if n_graded else None,   # flat 1u stake
        "units_pnl": round(pnl, 3),
        "breakeven_110": 0.524,
    }


def betting_backtest(preds: pd.DataFrame, lines: pd.DataFrame, cfg=CONFIG) -> Optional[dict]:
    """Honest 'would this have made money?' report: ATS hit-rate + flat-stake
    ROI under the live pick policy, a min_edge sensitivity sweep, and the
    straight-up winner-prediction accuracy. Built only on walk-forward
    out-of-time predictions, so it cannot flatter itself with future data."""
    if preds.empty:
        return None
    grid = sorted({0.02, 0.03, 0.04, 0.05, 0.06, round(float(cfg.MIN_EDGE), 4)})
    sweep = [_simulate_policy(preds, lines, e, cfg) for e in grid]
    return {
        "ats": _simulate_policy(preds, lines, cfg.MIN_EDGE, cfg),  # at the live threshold
        "edge_sweep": sweep,
        "straight_up": straight_up_accuracy(preds),
    }


def split_tune_report(preds: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Hyperparams are selected on the earliest evaluated season; the rest is
    the untouched report segment. With one season, split it in half by date."""
    if preds.empty:
        return preds, preds
    seasons = sorted(preds["season"].unique())
    if len(seasons) >= 2:
        tune = preds[preds["season"] == seasons[0]]
        report = preds[preds["season"] != seasons[0]]
    else:
        cutoff = preds["date"].sort_values().iloc[len(preds) // 2]
        tune = preds[preds["date"] <= cutoff]
        report = preds[preds["date"] > cutoff]
    return tune, report


def check_gates(report_margin: Optional[dict], baseline_margin: Optional[dict],
                report_cover: Optional[dict], team_coverage_ok: bool,
                cfg=CONFIG) -> Tuple[bool, List[str]]:
    reasons = []
    if not report_margin:
        return False, ["no report-segment predictions"]
    if report_margin["mae"] > cfg.GATE_MAE_ABS:
        reasons.append(f"G1: MAE {report_margin['mae']:.2f} > {cfg.GATE_MAE_ABS}")
    if baseline_margin and report_margin["mae"] > baseline_margin["mae"] - cfg.GATE_MAE_VS_BASELINE:
        reasons.append(
            f"G1: MAE {report_margin['mae']:.2f} not better than baseline "
            f"{baseline_margin['mae']:.2f} by {cfg.GATE_MAE_VS_BASELINE}")
    if report_cover:
        if report_cover["brier"] > cfg.GATE_BRIER_MAX:
            reasons.append(f"G2: Brier {report_cover['brier']:.4f} > {cfg.GATE_BRIER_MAX}")
        slope = report_cover.get("cal_slope")
        if slope is not None and not (cfg.GATE_CAL_SLOPE[0] <= slope <= cfg.GATE_CAL_SLOPE[1]):
            reasons.append(f"G2: calibration slope {slope:.2f} outside {cfg.GATE_CAL_SLOPE}")
    if abs(report_margin["bias"]) > cfg.GATE_BIAS_ABS:
        reasons.append(f"G3: |bias| {abs(report_margin['bias']):.2f} > {cfg.GATE_BIAS_ABS}")
    if not team_coverage_ok:
        reasons.append("G3: not all 30 teams present in eligible training rows")
    return (len(reasons) == 0), reasons


def team_coverage(df: pd.DataFrame) -> bool:
    el = df[df["eligible"]]
    teams = set(el["home_abbr"]) | set(el["away_abbr"])
    return len(teams) == 30


def evaluate_kind(df: pd.DataFrame, lines: pd.DataFrame, kind: str,
                  ridge_alpha: float = CONFIG.RIDGE_ALPHA,
                  hgb_iter: int = CONFIG.HGB_MAX_ITER) -> dict:
    preds = walk_forward(df, kind, ridge_alpha=ridge_alpha, hgb_iter=hgb_iter)
    tune, report = split_tune_report(preds)
    return {
        "kind": kind,
        "preds": preds,
        "tune_margin": margin_metrics(tune),
        "report_margin": margin_metrics(report),
        "report_cover": cover_metrics(report, lines),
        "report_betting": betting_backtest(preds, lines),
    }


# ===== totals (大小分) analogs =====

def total_metrics(preds: pd.DataFrame) -> Optional[dict]:
    if preds.empty:
        return None
    err = preds["total"] - preds["mu"]
    return {
        "n": int(len(preds)),
        "mae": float(err.abs().mean()),
        "rmse": float(np.sqrt((err ** 2).mean())),
        "bias": float(err.mean()),
        "resid_sigma": float(err.std(ddof=1)),
    }


def load_closing_totals(conn) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT l.game_id, l.total_line, l.over_price, l.under_price
            FROM public.v_closing_lines l
            JOIN public.games_v2 g ON g.game_id = l.game_id
            WHERE g.status = 'final' AND g.margin IS NOT NULL AND l.total_line IS NOT NULL
        """)
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=["game_id", "total_line", "over_price", "under_price"])


def over_metrics(preds: pd.DataFrame, lines: pd.DataFrame) -> Optional[dict]:
    """Brier/log-loss/calibration of derived over probs on lined games."""
    if preds.empty or lines.empty:
        return None
    df = preds.merge(lines, on="game_id", how="inner")
    if df.empty:
        return None

    records = []
    for _, r in df.iterrows():
        pr = total_to_over_prob(r["mu"], r["sigma"], r["total_line"])
        actual = r["total"] - r["total_line"]
        if abs(actual) < 1e-9:
            continue  # push: refunded, excluded
        y = 1.0 if actual > 0 else 0.0
        rec = {"p": pr["p_raw"], "y": y}
        if r["over_price"] and r["under_price"]:
            rec["p_market"] = devig(r["over_price"], r["under_price"])[0]
        records.append(rec)
    if len(records) < 20:
        return None

    e = pd.DataFrame(records)
    p, y = e["p"].values, e["y"].values
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    out = {
        "n": int(len(e)),
        "brier": float(((p - y) ** 2).mean()),
        "logloss": float(-(y * np.log(pc) + (1 - y) * np.log(1 - pc)).mean()),
        "brier_p50": 0.25,
        "accuracy": float(((p >= 0.5).astype(float) == y).mean()),  # O/U pick hit-rate
    }
    if "p_market" in e and e["p_market"].notna().sum() >= 20:
        pm = e["p_market"].dropna()
        ym = e.loc[pm.index, "y"]
        out["brier_market"] = float(((pm - ym) ** 2).mean())
    try:
        from sklearn.linear_model import LogisticRegression
        lz = np.log(pc / (1 - pc)).reshape(-1, 1)
        lr = LogisticRegression(C=1e9, solver="lbfgs").fit(lz, y)
        out["cal_slope"] = float(lr.coef_[0][0])
        out["cal_intercept"] = float(lr.intercept_[0])
    except Exception:
        pass
    return out


def check_total_gates(report_total: Optional[dict], baseline_total: Optional[dict],
                      report_over: Optional[dict], team_coverage_ok: bool,
                      cfg=CONFIG) -> Tuple[bool, List[str]]:
    reasons = []
    if not report_total:
        return False, ["no report-segment total predictions"]
    if report_total["mae"] > cfg.GATE_TOTAL_MAE_ABS:
        reasons.append(f"T1: total MAE {report_total['mae']:.2f} > {cfg.GATE_TOTAL_MAE_ABS}")
    if baseline_total and report_total["mae"] > baseline_total["mae"] - cfg.GATE_MAE_VS_BASELINE:
        reasons.append(
            f"T1: total MAE {report_total['mae']:.2f} not better than baseline "
            f"{baseline_total['mae']:.2f} by {cfg.GATE_MAE_VS_BASELINE}")
    if report_over:
        if report_over["brier"] > cfg.GATE_BRIER_MAX:
            reasons.append(f"T2: over Brier {report_over['brier']:.4f} > {cfg.GATE_BRIER_MAX}")
        slope = report_over.get("cal_slope")
        if slope is not None and not (cfg.GATE_CAL_SLOPE[0] <= slope <= cfg.GATE_CAL_SLOPE[1]):
            reasons.append(f"T2: over calibration slope {slope:.2f} outside {cfg.GATE_CAL_SLOPE}")
    if abs(report_total["bias"]) > cfg.GATE_BIAS_ABS:
        reasons.append(f"T3: |total bias| {abs(report_total['bias']):.2f} > {cfg.GATE_BIAS_ABS}")
    if not team_coverage_ok:
        reasons.append("T3: not all 30 teams present in eligible training rows")
    return (len(reasons) == 0), reasons


def evaluate_total_kind(df: pd.DataFrame, lines_total: pd.DataFrame, kind: str,
                        ridge_alpha: float = CONFIG.RIDGE_ALPHA,
                        hgb_iter: int = CONFIG.HGB_MAX_ITER) -> dict:
    preds = walk_forward(df, kind, ridge_alpha=ridge_alpha, hgb_iter=hgb_iter,
                         features=TOTAL_FEATURE_NAMES, target="total")
    tune, report = split_tune_report(preds)
    return {
        "kind": kind,
        "preds": preds,
        "tune_total": total_metrics(tune),
        "report_total": total_metrics(report),
        "report_over": over_metrics(report, lines_total),
    }


def main() -> None:
    conn = db_connect()
    try:
        bundles = load_bundles(conn)
        if not bundles:
            print("[WARN] no final games with boxscores; nothing to evaluate", flush=True)
            return
        df = build_feature_table(bundles)
        lines = load_closing_lines(conn)
    finally:
        conn.close()

    print(f"[INFO] feature table: {len(df)} games, eligible={int(df['eligible'].sum())}, "
          f"lined finals={len(lines)}", flush=True)

    report = {"team_coverage_ok": team_coverage(df)}
    for kind in BASELINE_KINDS + CONFIG.MODEL_CANDIDATES:
        r = evaluate_kind(df, lines, kind)
        report[kind] = {
            "tune_margin": r["tune_margin"],
            "report_margin": r["report_margin"],
            "report_cover": r["report_cover"],
            "report_betting": r["report_betting"],
        }
        rm = r["report_margin"]
        print(f"[EVAL] {kind}: report MAE={rm['mae']:.3f} bias={rm['bias']:+.3f} "
              f"n={rm['n']}" if rm else f"[EVAL] {kind}: insufficient data", flush=True)
        rb = r["report_betting"]
        if rb:
            ats, su = rb["ats"], rb["straight_up"]
            hit = f"{ats['hit_rate'] * 100:.1f}%" if ats.get("hit_rate") is not None else "n/a"
            roi = f"{ats['roi'] * 100:+.1f}%" if ats.get("roi") is not None else "n/a"
            su_acc = f"{su['winner_accuracy'] * 100:.1f}%" if su else "n/a"
            print(f"[BET ] {kind}: ATS picks={ats['n_graded']} hit={hit} ROI={roi} "
                  f"(breakeven 52.4%) | winner-acc={su_acc}", flush=True)

    print(json.dumps(report, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
