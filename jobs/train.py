"""Weekly/on-demand retrain: rebuild features → walk-forward bake-off →
acceptance gates → fit on all eligible rows → registry write + promotion.

Run: python -m jobs.train
"""
from __future__ import annotations

import sys
from typing import Optional

import numpy as np

from jobs.config import CONFIG, CONFIG_HASH
from jobs.db_utils import db_connect
from jobs.evaluate import (check_gates, check_total_gates, evaluate_kind,
                           evaluate_total_kind, load_closing_lines,
                           load_closing_totals, margin_metrics, split_tune_report,
                           team_coverage, total_metrics, walk_forward)
from jobs.features import (FEATURE_NAMES, TOTAL_FEATURE_NAMES,
                           build_feature_table, load_bundles)
from jobs.model import (MODEL_NAME, TOTAL_MODEL_NAME, build_payload,
                        estimate_sigma, fit_candidate, load_active, next_version,
                        save_model)
from jobs.schema import ensure_schema


def tune_hyperparams(df, lines, kind: str):
    """Pick ridge alpha / hgb iters by tune-segment MAE (report stays untouched)."""
    if kind == "ridge":
        grid = [("ridge_alpha", a) for a in CONFIG.RIDGE_ALPHA_GRID]
    elif kind == "hgb":
        grid = [("hgb_iter", i) for i in CONFIG.HGB_ITER_GRID]
    else:
        return {}

    best_param, best_mae = None, None
    for pname, pval in grid:
        preds = walk_forward(df, kind, **{pname: pval})
        tune, _ = split_tune_report(preds)
        m = margin_metrics(tune)
        if m is None:
            continue
        if best_mae is None or m["mae"] < best_mae:
            best_mae, best_param = m["mae"], (pname, pval)
        print(f"[TUNE] {kind} {pname}={pval}: tune MAE={m['mae']:.3f}", flush=True)
    return dict([best_param]) if best_param else {}


def score_key(result: dict):
    """Bake-off winner: cover-Brier on lined report games when available,
    else report MAE."""
    cov, mar = result["report_cover"], result["report_margin"]
    if cov is not None:
        return (0, cov["brier"])
    return (1, mar["mae"] if mar else float("inf"))


def tune_hyperparams_total(df, kind: str):
    """Pick ridge alpha / hgb iters by tune-segment total MAE."""
    if kind == "ridge":
        grid = [("ridge_alpha", a) for a in CONFIG.RIDGE_ALPHA_GRID]
    elif kind == "hgb":
        grid = [("hgb_iter", i) for i in CONFIG.HGB_ITER_GRID]
    else:
        return {}
    best_param, best_mae = None, None
    for pname, pval in grid:
        preds = walk_forward(df, kind, features=TOTAL_FEATURE_NAMES, target="total",
                             **{pname: pval})
        tune, _ = split_tune_report(preds)
        m = total_metrics(tune)
        if m is None:
            continue
        if best_mae is None or m["mae"] < best_mae:
            best_mae, best_param = m["mae"], (pname, pval)
        print(f"[TUNE] total {kind} {pname}={pval}: tune MAE={m['mae']:.3f}", flush=True)
    return dict([best_param]) if best_param else {}


def _score_key_total(result: dict):
    ov, to = result["report_over"], result["report_total"]
    if ov is not None:
        return (0, ov["brier"])
    return (1, to["mae"] if to else float("inf"))


def train_totals(conn, df) -> bool:
    """Train + (maybe) promote the totals model. Returns True on promote/no-op,
    False only when a trained candidate failed gates (so the run exits non-zero)."""
    el = df[df["eligible"] & df["total"].notna()]
    if int(len(el)) < CONFIG.MIN_TRAIN_ROWS:
        print(f"[WARN] totals: only {len(el)} eligible rows (< {CONFIG.MIN_TRAIN_ROWS}); "
              f"skipping totals model", flush=True)
        return True
    lines_t = load_closing_totals(conn)
    print(f"[INFO] totals eligible rows={len(el)} lined totals={len(lines_t)}", flush=True)

    baseline = evaluate_total_kind(df, lines_t, "const")
    print(f"[BASE] total const: {baseline['report_total']}", flush=True)

    results = []
    for kind in CONFIG.MODEL_CANDIDATES:
        params = tune_hyperparams_total(df, kind)
        r = evaluate_total_kind(df, lines_t, kind, **params)
        r["params"] = params
        results.append(r)
        print(f"[BAKE] total {kind} params={params} report_total={r['report_total']} "
              f"report_over={r['report_over']}", flush=True)
    winner = min(results, key=_score_key_total)
    kind = winner["kind"]
    print(f"[INFO] totals winner={kind} params={winner['params']}", flush=True)

    passed, reasons = check_total_gates(
        winner["report_total"], baseline["report_total"],
        winner["report_over"], team_coverage(df))
    if not passed:
        print(f"[WARN] total gates failed: {reasons}", flush=True)

    _, active_metrics = load_active(conn, TOTAL_MODEL_NAME)
    promote = passed
    if passed and active_metrics:
        new_ov, old_ov = winner["report_over"], active_metrics.get("report_over")
        if new_ov and old_ov and new_ov["brier"] > old_ov["brier"] + CONFIG.PROMOTION_BRIER_TOL:
            promote = False
            reasons.append(
                f"promotion: total Brier {new_ov['brier']:.4f} degrades active "
                f"{old_ov['brier']:.4f}")
            print(f"[WARN] {reasons[-1]}", flush=True)

    model = fit_candidate(kind, el[TOTAL_FEATURE_NAMES].values, el["total"].values,
                          **winner["params"])
    oof = winner["preds"]
    sigma = estimate_sigma((oof["total"] - oof["mu"]).values)

    version = next_version(conn, TOTAL_MODEL_NAME)
    payload = build_payload(model, TOTAL_FEATURE_NAMES, sigma, kind,
                            el["date"].max(), {
                                "report_total": winner["report_total"],
                                "report_over": winner["report_over"],
                            })
    metrics = {
        "sigma": sigma,
        "model_kind": kind,
        "params": winner["params"],
        "config_hash": CONFIG_HASH,
        "report_total": winner["report_total"],
        "report_over": winner["report_over"],
        "baseline_const": baseline["report_total"],
        "gates_passed": passed,
        "gate_reasons": reasons,
    }
    save_model(conn, TOTAL_MODEL_NAME, version, payload, TOTAL_FEATURE_NAMES,
               int(len(el)), metrics, activate=promote)
    state = "ACTIVE" if promote else "inactive"
    print(f"[OK] saved {TOTAL_MODEL_NAME} {version} ({state}) kind={kind} "
          f"sigma={sigma:.2f} rows={len(el)}", flush=True)
    return promote


def main() -> None:
    conn = db_connect()
    try:
        ensure_schema(conn)
        bundles = load_bundles(conn)
        df = build_feature_table(bundles) if bundles else None
        if df is None or int(df["eligible"].sum()) < CONFIG.MIN_TRAIN_ROWS:
            n = 0 if df is None else int(df["eligible"].sum())
            print(f"[WARN] only {n} eligible rows (< {CONFIG.MIN_TRAIN_ROWS}); "
                  f"not training", flush=True)
            return
        lines = load_closing_lines(conn)
        print(f"[INFO] eligible rows={int(df['eligible'].sum())} "
              f"lined finals={len(lines)}", flush=True)

        # baselines first — they validate the harness and feed gate G1
        baseline = evaluate_kind(df, lines, "const")
        elo_only = evaluate_kind(df, lines, "elo")
        print(f"[BASE] const: {baseline['report_margin']}", flush=True)
        print(f"[BASE] elo:   {elo_only['report_margin']}", flush=True)

        # candidate bake-off
        results = []
        hyper = {}
        for kind in CONFIG.MODEL_CANDIDATES:
            params = tune_hyperparams(df, lines, kind)
            r = evaluate_kind(df, lines, kind, **params)
            r["params"] = params
            results.append(r)
            print(f"[BAKE] {kind} params={params} report_margin={r['report_margin']} "
                  f"report_cover={r['report_cover']}", flush=True)
        winner = min(results, key=score_key)
        kind = winner["kind"]
        print(f"[INFO] winner={kind} params={winner['params']}", flush=True)

        # gates
        passed, reasons = check_gates(
            winner["report_margin"], baseline["report_margin"],
            winner["report_cover"], team_coverage(df))
        if not passed:
            print(f"[WARN] gates failed: {reasons}", flush=True)

        # promotion guard vs the currently active version
        _, active_metrics = load_active(conn, MODEL_NAME)
        promote = passed
        if passed and active_metrics:
            new_cov, old_cov = winner["report_cover"], active_metrics.get("report_cover")
            if new_cov and old_cov and new_cov["brier"] > old_cov["brier"] + CONFIG.PROMOTION_BRIER_TOL:
                promote = False
                reasons.append(
                    f"promotion: Brier {new_cov['brier']:.4f} degrades active "
                    f"{old_cov['brier']:.4f} by > {CONFIG.PROMOTION_BRIER_TOL}")
                print(f"[WARN] {reasons[-1]}", flush=True)

        # final fit on all eligible rows; sigma from walk-forward OOF residuals
        el = df[df["eligible"]]
        model = fit_candidate(kind, el[FEATURE_NAMES].values, el["margin"].values,
                              **winner["params"])
        oof = winner["preds"]
        sigma = estimate_sigma((oof["margin"] - oof["mu"]).values)

        version = next_version(conn, MODEL_NAME)
        payload = build_payload(model, FEATURE_NAMES, sigma, kind,
                                el["date"].max(), {
                                    "report_margin": winner["report_margin"],
                                    "report_cover": winner["report_cover"],
                                })
        metrics = {
            "sigma": sigma,
            "model_kind": kind,
            "params": winner["params"],
            "config_hash": CONFIG_HASH,
            "report_margin": winner["report_margin"],
            "report_cover": winner["report_cover"],
            "report_betting": winner["report_betting"],
            "baseline_const": baseline["report_margin"],
            "baseline_elo": elo_only["report_margin"],
            "gates_passed": passed,
            "gate_reasons": reasons,
        }
        save_model(conn, MODEL_NAME, version, payload, FEATURE_NAMES,
                   int(len(el)), metrics, activate=promote)
        state = "ACTIVE" if promote else "inactive"
        print(f"[OK] saved {MODEL_NAME} {version} ({state}) kind={kind} "
              f"sigma={sigma:.2f} rows={len(el)}", flush=True)

        # ----- totals model (additive; mirrors the margin flow) -----
        print("===== [train] totals model =====", flush=True)
        totals_ok = train_totals(conn, df)

        if not promote or not totals_ok:
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
