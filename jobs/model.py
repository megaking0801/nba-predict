"""Margin models, the margin→cover-probability head, de-vigging, and model
registry (de)serialization.

Stage 2 math (conventions match settlement: home covers ⇔ margin + spread > 0):
  non-integer spread:  p_win = 1 − Φ((−s − μ)/σ),  p_push = 0
  integer spread:      continuity-corrected push mass at ±0.5
  p_raw = p_win / (p_win + p_loss)         (conditional on no push, like the
                                            market's implied probs, since
                                            pushes refund)
  EV    = p_win·(d − 1) − p_loss
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import math
import pickle
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from jobs.config import CONFIG, CONFIG_HASH

MODEL_NAME = "margin_model"
CALIBRATOR_NAME = "cover_calibrator"
TOTAL_MODEL_NAME = "total_model"
OVER_CALIBRATOR_NAME = "over_calibrator"


# ----- margin models -----

class BlendModel:
    """Average of two fitted regressors; top-level class so it pickles."""

    def __init__(self, models: Sequence):
        self.models = list(models)

    def predict(self, X):
        preds = [m.predict(X) for m in self.models]
        return np.mean(preds, axis=0)


def fit_ridge(X, y, sample_weight=None, alpha: float = CONFIG.RIDGE_ALPHA):
    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("ridge", Ridge(alpha=alpha)),
    ])
    pipe.fit(X, y, ridge__sample_weight=sample_weight)
    return pipe


def fit_hgb(X, y, sample_weight=None, max_iter: int = CONFIG.HGB_MAX_ITER):
    m = HistGradientBoostingRegressor(
        loss="squared_error",
        learning_rate=CONFIG.HGB_LEARNING_RATE,
        max_depth=CONFIG.HGB_MAX_DEPTH,
        max_leaf_nodes=CONFIG.HGB_MAX_LEAF_NODES,
        min_samples_leaf=CONFIG.HGB_MIN_SAMPLES_LEAF,
        l2_regularization=CONFIG.HGB_L2,
        max_iter=max_iter,
        early_stopping=False,   # built-in stopping uses a random split: leakage
        random_state=CONFIG.RANDOM_STATE,
    )
    m.fit(X, y, sample_weight=sample_weight)
    return m


def fit_candidate(kind: str, X, y, sample_weight=None,
                  ridge_alpha: float = CONFIG.RIDGE_ALPHA,
                  hgb_iter: int = CONFIG.HGB_MAX_ITER):
    if kind == "ridge":
        return fit_ridge(X, y, sample_weight, alpha=ridge_alpha)
    if kind == "hgb":
        return fit_hgb(X, y, sample_weight, max_iter=hgb_iter)
    if kind == "blend":
        return BlendModel([fit_ridge(X, y, sample_weight, alpha=ridge_alpha),
                           fit_hgb(X, y, sample_weight, max_iter=hgb_iter)])
    raise ValueError(f"unknown model kind: {kind}")


def estimate_sigma(residuals: Sequence[float], method: str = CONFIG.SIGMA_ESTIMATOR) -> float:
    r = np.asarray(list(residuals), dtype=float)
    if r.size < 2:
        return 13.0  # NBA margin stdev ballpark; only hit with absurdly little data
    if method == "mad":
        return float(1.4826 * np.median(np.abs(r - np.median(r))))
    return float(np.std(r, ddof=1))


# ----- probability head -----

def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def margin_to_cover_prob(mu: float, sigma: float, spread: float) -> Dict[str, float]:
    """P(home covers spread s) for home margin M ~ Normal(mu, sigma).

    Home covers ⇔ M + s > 0 ⇔ M > −s. Integer spreads get a continuity-
    corrected push mass because margins are integers.
    """
    sigma = max(1e-6, float(sigma))
    threshold = -float(spread)
    z = lambda m: (m - mu) / sigma
    if abs(spread - round(spread)) < 1e-9:
        p_win = 1.0 - _phi(z(threshold + 0.5))
        p_push = _phi(z(threshold + 0.5)) - _phi(z(threshold - 0.5))
        p_loss = _phi(z(threshold - 0.5))
    else:
        p_win = 1.0 - _phi(z(threshold))
        p_push = 0.0
        p_loss = 1.0 - p_win
    denom = p_win + p_loss
    p_raw = p_win / denom if denom > 0 else 0.5
    return {"p_win": p_win, "p_push": p_push, "p_loss": p_loss, "p_raw": p_raw}


def total_to_over_prob(mu: float, sigma: float, total_line: float) -> Dict[str, float]:
    """P(game total goes OVER the line) for total T ~ Normal(mu, sigma).

    Over ⇔ T > line. Integer lines get a continuity-corrected push mass because
    totals are integers (mirrors margin_to_cover_prob). p_raw is conditional on
    no push, matching the market's implied probs (pushes refund)."""
    sigma = max(1e-6, float(sigma))
    line = float(total_line)
    z = lambda t: (t - mu) / sigma
    if abs(line - round(line)) < 1e-9:
        p_over = 1.0 - _phi(z(line + 0.5))
        p_push = _phi(z(line + 0.5)) - _phi(z(line - 0.5))
        p_under = _phi(z(line - 0.5))
    else:
        p_over = 1.0 - _phi(z(line))
        p_push = 0.0
        p_under = 1.0 - p_over
    denom = p_over + p_under
    p_raw = p_over / denom if denom > 0 else 0.5
    return {"p_over": p_over, "p_push": p_push, "p_under": p_under, "p_raw": p_raw}


def margin_to_win_prob(mu: float, sigma: float) -> float:
    """P(home wins straight up) for home margin M ~ Normal(mu, sigma).

    Home wins ⇔ M > 0. NBA games cannot tie, so there is no push mass and no
    continuity correction: p_home_win = 1 − Φ(−mu/sigma) = Φ(mu/sigma)."""
    sigma = max(1e-6, float(sigma))
    return _phi(float(mu) / sigma)


def expected_value(p_win: float, p_loss: float, decimal_odds: float) -> float:
    """Flat-stake EV per unit; pushes refund the stake."""
    return p_win * (decimal_odds - 1.0) - p_loss


def devig(home_price: float, away_price: float) -> Tuple[float, float]:
    """Multiplicative de-vig: two-sided fair probabilities summing to 1."""
    q_h, q_a = 1.0 / home_price, 1.0 / away_price
    overround = q_h + q_a
    return q_h / overround, q_a / overround


# ----- forward calibration application -----

def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def apply_calibrator(calibrator: Optional[dict], p_raw: float) -> float:
    """calibrator: {'type':'identity'} | {'type':'platt','a':..,'b':..}
    | {'type':'isotonic','model':IsotonicRegression}"""
    if not calibrator or calibrator.get("type") == "identity":
        return p_raw
    if calibrator["type"] == "platt":
        z = calibrator["a"] * _logit(p_raw) + calibrator["b"]
        return 1.0 / (1.0 + math.exp(-z))
    if calibrator["type"] == "isotonic":
        return float(np.clip(calibrator["model"].predict([p_raw])[0], 0.001, 0.999))
    raise ValueError(f"unknown calibrator type: {calibrator.get('type')}")


# ----- payload + registry -----

def build_payload(model, feature_names: List[str], sigma: float,
                  model_kind: str, train_through_date: dt.date,
                  oof_metrics: dict) -> dict:
    return {
        "model": model,
        "model_kind": model_kind,
        "feature_names": list(feature_names),
        "sigma": float(sigma),
        "config_hash": CONFIG_HASH,
        "train_through_date": train_through_date.isoformat(),
        "oof_metrics": oof_metrics,
    }


def serialize(obj) -> str:
    return base64.b64encode(pickle.dumps(obj)).decode("ascii")


def deserialize(payload_base64: str):
    return pickle.loads(base64.b64decode(payload_base64))


def next_version(conn, model_name: str, today_utc: Optional[dt.date] = None) -> str:
    d = today_utc or dt.datetime.now(dt.timezone.utc).date()
    tag = d.strftime("%Y.%m.%d")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM public.model_registry_v2 "
            "WHERE model_name = %s AND model_version LIKE %s",
            (model_name, f"{tag}-r%"),
        )
        n = cur.fetchone()[0]
    return f"{tag}-r{n + 1}"


def save_model(conn, model_name: str, version: str, payload: dict,
               feature_names: Optional[List[str]], trained_rows: int,
               metrics: dict, activate: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.model_registry_v2
              (model_name, model_version, payload_base64, feature_set,
               feature_names, trained_rows, metrics, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
            """,
            (model_name, version, serialize(payload), CONFIG.FEATURE_SET,
             json.dumps(feature_names) if feature_names else None,
             trained_rows, json.dumps(metrics)),
        )
        if activate:
            cur.execute(
                "UPDATE public.model_registry_v2 SET is_active = FALSE "
                "WHERE model_name = %s AND is_active",
                (model_name,),
            )
            cur.execute(
                "UPDATE public.model_registry_v2 SET is_active = TRUE "
                "WHERE model_name = %s AND model_version = %s",
                (model_name, version),
            )
    conn.commit()


def load_active(conn, model_name: str) -> Tuple[Optional[dict], Optional[dict]]:
    """Returns (payload, metrics) of the active version, or (None, None)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload_base64, metrics FROM public.model_registry_v2 "
            "WHERE model_name = %s AND is_active",
            (model_name,),
        )
        row = cur.fetchone()
    if not row:
        return None, None
    metrics = row[1] if isinstance(row[1], dict) else json.loads(row[1] or "{}")
    return deserialize(row[0]), metrics
