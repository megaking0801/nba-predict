"""Forward calibration (Stage 3): map the model's p_raw onto observed cover
outcomes as lined, settled predictions accumulate.

Identity below CAL_MIN_N_PLATT; Platt scaling (2 params, robust at small N)
up to CAL_MIN_N_ISOTONIC; beyond that isotonic is promoted only if it beats
Platt on a time-ordered 80/20 split — never random KFold (the old leakage).

Run: python -m jobs.calibrate
"""
from __future__ import annotations

import json
from typing import List, Optional, Tuple

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from jobs.config import CONFIG, CONFIG_HASH
from jobs.db_utils import db_connect
from jobs.model import CALIBRATOR_NAME, apply_calibrator, next_version, save_model
from jobs.schema import ensure_schema


def load_calibration_set(conn) -> Tuple[np.ndarray, np.ndarray]:
    """(p_raw, cover) from the latest settled, lined prediction per game,
    time-ordered. Pushes excluded."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p_raw, cover_result FROM (
                SELECT DISTINCT ON (p.game_id)
                       p.game_id, p.p_raw, p.cover_result, p.predicted_at
                FROM public.predictions p
                WHERE p.p_raw IS NOT NULL
                  AND p.home_spread_used IS NOT NULL
                  AND p.cover_result IN (0, 1)
                ORDER BY p.game_id, p.predicted_at DESC
            ) t
            ORDER BY t.predicted_at
        """)
        rows = cur.fetchall()
    if not rows:
        return np.array([]), np.array([])
    p = np.array([r[0] for r in rows], dtype=float)
    y = np.array([r[1] for r in rows], dtype=float)
    return p, y


def _logit(p: np.ndarray) -> np.ndarray:
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(pc / (1 - pc))


def fit_platt(p: np.ndarray, y: np.ndarray) -> dict:
    lr = LogisticRegression(C=1e9, solver="lbfgs")
    lr.fit(_logit(p).reshape(-1, 1), y)
    return {"type": "platt", "a": float(lr.coef_[0][0]), "b": float(lr.intercept_[0])}


def fit_isotonic(p: np.ndarray, y: np.ndarray) -> dict:
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.001, y_max=0.999)
    iso.fit(p, y)
    return {"type": "isotonic", "model": iso}


def brier(cal: dict, p: np.ndarray, y: np.ndarray) -> float:
    preds = np.array([apply_calibrator(cal, float(x)) for x in p])
    return float(((preds - y) ** 2).mean())


def choose_calibrator(p: np.ndarray, y: np.ndarray, cfg=CONFIG) -> Tuple[dict, dict]:
    """Returns (calibrator, metrics)."""
    n = len(p)
    metrics: dict = {"n": n, "config_hash": CONFIG_HASH}

    if n < cfg.CAL_MIN_N_PLATT:
        metrics["method"] = "identity"
        return {"type": "identity"}, metrics

    platt = fit_platt(p, y)
    metrics.update({"platt_a": platt["a"], "platt_b": platt["b"]})
    alarm = not (cfg.CAL_SLOPE_ALARM[0] <= platt["a"] <= cfg.CAL_SLOPE_ALARM[1]) \
        or abs(platt["b"]) > cfg.CAL_INTERCEPT_ALARM
    metrics["alarm"] = alarm
    if alarm:
        print(f"[WARN] calibration alarm: a={platt['a']:.2f} b={platt['b']:.2f} "
              f"(picks threshold tightens to {cfg.MIN_EDGE_ALARM_MODE})", flush=True)

    if n < cfg.CAL_MIN_N_ISOTONIC:
        metrics["method"] = "platt"
        return platt, metrics

    # time-ordered 80/20: fit on the older 80%, compare on the newest 20%
    cut = int(n * 0.8)
    candidates = {
        "identity": {"type": "identity"},
        "platt": fit_platt(p[:cut], y[:cut]),
        "isotonic": fit_isotonic(p[:cut], y[:cut]),
    }
    scores = {name: brier(c, p[cut:], y[cut:]) for name, c in candidates.items()}
    metrics["holdout_brier"] = scores
    best = min(scores, key=scores.get)
    metrics["method"] = best
    if best == "identity":
        return {"type": "identity"}, metrics
    refit = fit_platt(p, y) if best == "platt" else fit_isotonic(p, y)
    return refit, metrics


def main() -> None:
    conn = db_connect()
    try:
        ensure_schema(conn)
        p, y = load_calibration_set(conn)
        calibrator, metrics = choose_calibrator(p, y)
        if len(p):
            metrics["brier_after"] = brier(calibrator, p, y)
        version = next_version(conn, CALIBRATOR_NAME)
        save_model(conn, CALIBRATOR_NAME, version, {"calibrator": calibrator},
                   None, int(len(p)), metrics, activate=True)
        print(f"[OK] saved {CALIBRATOR_NAME} {version} method={metrics['method']} "
              f"n={len(p)}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
