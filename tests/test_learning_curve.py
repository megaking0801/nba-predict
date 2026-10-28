"""jobs.learning_curve: windowed walk-forward and curve bookkeeping (synthetic data, no DB)."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from jobs import learning_curve as lc
from jobs.config import CONFIG
from jobs.evaluate import walk_forward
from jobs.features import FEATURE_NAMES


def _df(n=2400, seed=0):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, len(FEATURE_NAMES)))
    margin = X[:, 0] * 3.0 + rng.normal(0, 12.0, n)
    dates = [dt.date(2018, 10, 20) + dt.timedelta(days=int(i // 6)) for i in range(n)]
    seasons = [f"{d.year if d.month >= 10 else d.year - 1}-{str((d.year if d.month >= 10 else d.year - 1) + 1)[2:]}"
               for d in dates]
    df = pd.DataFrame(X, columns=FEATURE_NAMES)
    df["elo_diff"] = X[:, 0] * 30
    df["game_id"] = [f"g{i}" for i in range(n)]
    df["date"], df["season"], df["season_type"] = dates, seasons, "regular"
    df["margin"], df["eligible"] = margin, True
    return df


def test_no_cap_reproduces_walk_forward():
    df = _df()
    a = lc.windowed_walk_forward(df, "ridge", None)
    b = walk_forward(df, "ridge")
    assert len(a) == len(b) > 0
    assert np.allclose(a["mu"].to_numpy(), b["mu"].to_numpy())
    assert np.allclose(a["sigma"].to_numpy(), b["sigma"].to_numpy())


def test_cap_limits_training_rows_and_uses_only_the_past():
    df = _df()
    p = lc.windowed_walk_forward(df, "ridge", 900)
    assert p["n_train"].max() <= 900 and p["n_train"].min() >= CONFIG.WF_MIN_TRAIN_ROWS
    assert len(p) == len(lc.windowed_walk_forward(df, "ridge", None))


def test_size_grid_is_log_spaced_and_ends_at_max():
    g = lc.size_grid(12000, 8)
    assert g[0] == CONFIG.WF_MIN_TRAIN_ROWS and g[-1] == 12000 and g == sorted(set(g))
    assert lc.size_grid(500) == [500]


def test_market_errors_use_negative_spread_as_expected_margin():
    sub = pd.DataFrame({"game_id": ["a"], "date": [dt.date(2024, 1, 1)], "margin": [7.0]})
    lines = pd.DataFrame({"game_id": ["a"], "home_spread": [-5.5]})      # home favoured by 5.5
    assert abs(lc.market_errors(sub, lines)["market_abs"].iloc[0] - 1.5) < 1e-9


def test_doubling_slope_recovers_a_log_linear_curve():
    pts = [{"n_train_mean": 800 * 2 ** k, "mae": 10.0 - 0.1 * k} for k in range(5)]
    assert abs(lc.doubling_slope(pts) + 0.1) < 1e-9
    assert lc.doubling_slope(pts[:2]) is None


def test_run_curve_on_synthetic_data_is_consistent():
    df = _df(3000)
    rng = np.random.default_rng(1)
    lines = pd.DataFrame({"game_id": df["game_id"], "home_spread": -df["elo_diff"] / 10 + rng.normal(0, 1, len(df)),
                          "home_price": 1.91, "away_price": 1.91})
    seg = {"first_season": "2019-20", "last_season": "2024-25", "season_types": ("regular",)}
    res = lc.run_curve(df, lines, [900, 1500, None], seg=seg, reps=50)
    r = res["curves"]["ridge"]["points"]
    assert [p["max_rows"] for p in r] == [900, 1500, None]
    assert len({p["n"] for p in r}) == 1 and r[0]["n"] == res["n_common_games"]
    assert abs(r[-1]["vs_full"]["mean"]) < 1e-12              # the full-size point against itself
    assert r[0]["n_train_mean"] < r[-1]["n_train_mean"]
    assert "Line's own MAE" in lc.text_table(res)
