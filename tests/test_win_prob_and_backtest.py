"""Straight-up win probability + betting-backtest accounting."""
import datetime as dt
import math

import pandas as pd
import pytest

from jobs.evaluate import _simulate_policy, betting_backtest, straight_up_accuracy
from jobs.model import margin_to_win_prob


# ----- margin_to_win_prob -----

def test_win_prob_even_at_zero_margin():
    assert math.isclose(margin_to_win_prob(0.0, 12.0), 0.5, abs_tol=1e-9)


def test_win_prob_favorite_around_eighty_percent():
    # Φ(10/12) ≈ 0.798 — a +10 expected margin team wins ~80% of the time
    assert math.isclose(margin_to_win_prob(10.0, 12.0), 0.7977, abs_tol=1e-3)


def test_win_prob_symmetry():
    assert math.isclose(margin_to_win_prob(7.0, 12.0) + margin_to_win_prob(-7.0, 12.0),
                        1.0, abs_tol=1e-12)


def test_win_prob_monotonic_in_mu():
    assert margin_to_win_prob(5.0, 12.0) > margin_to_win_prob(2.0, 12.0)


# ----- straight_up_accuracy -----

def test_straight_up_accuracy_counts_correct_winners():
    preds = pd.DataFrame({
        "mu":     [8.0, -5.0, 2.0, -1.0],
        "margin": [8,    3,    2,   -1],
    })
    su = straight_up_accuracy(preds)
    assert su["n"] == 4
    # predicted home for rows 0,2; correct on 0,2,3 (row1 predicts away, home actually won)
    assert math.isclose(su["winner_accuracy"], 0.75, abs_tol=1e-9)


# ----- betting backtest accounting -----

def _preds(rows):
    return pd.DataFrame(rows, columns=["game_id", "date", "season", "margin", "mu", "sigma"])


def _lines(rows):
    return pd.DataFrame(rows, columns=["game_id", "home_spread", "home_price", "away_price"])


def test_simulate_policy_winning_pick_and_push():
    d = dt.date(2025, 1, 1)
    preds = _preds([
        ("g1", d, "2024-25", 8, 8.0, 12.0),   # home -3, wins by 8 → covers → win
        ("g2", d, "2024-25", 3, 8.0, 12.0),   # home -3, wins by 3 → actual 0 → push
    ])
    lines = _lines([
        ("g1", -3.0, 2.0, 2.0),
        ("g2", -3.0, 2.0, 2.0),
    ])
    r = _simulate_policy(preds, lines, min_edge=0.04)
    assert r["n_picks"] == 2          # both clear the edge → both picked
    assert r["pushes"] == 1
    assert r["n_graded"] == 1         # push excluded from grading
    assert math.isclose(r["hit_rate"], 1.0, abs_tol=1e-9)
    assert math.isclose(r["units_pnl"], 1.0, abs_tol=1e-9)   # +1u at 2.0 odds
    assert math.isclose(r["roi"], 1.0, abs_tol=1e-9)


def test_simulate_policy_abstains_without_edge():
    d = dt.date(2025, 1, 1)
    # fair line: spread matches mu and prices imply ~the model's own prob → no edge
    preds = _preds([("g1", d, "2024-25", 0, 0.0, 12.0)])
    lines = _lines([("g1", -0.5, 1.91, 1.91)])
    r = _simulate_policy(preds, lines, min_edge=0.04)
    assert r["n_picks"] == 0
    assert r["hit_rate"] is None


def test_betting_backtest_shape_and_sweep():
    d = dt.date(2025, 1, 1)
    preds = _preds([("g1", d, "2024-25", 8, 8.0, 12.0)])
    lines = _lines([("g1", -3.0, 2.0, 2.0)])
    rb = betting_backtest(preds, lines)
    assert set(rb) == {"ats", "edge_sweep", "straight_up"}
    assert rb["straight_up"]["winner_accuracy"] == 1.0
    # sweep covers the documented thresholds
    sweep_edges = {row["min_edge"] for row in rb["edge_sweep"]}
    assert {0.02, 0.03, 0.04, 0.05, 0.06}.issubset(sweep_edges)


def test_missing_price_falls_back_to_dash110():
    d = dt.date(2025, 1, 1)
    # no prices → decide_game can't compute edge → abstains (no_prices), so no pick.
    # This documents that price-less lines never produce a graded bet.
    preds = _preds([("g1", d, "2024-25", 8, 8.0, 12.0)])
    lines = _lines([("g1", -3.0, None, None)])
    r = _simulate_policy(preds, lines, min_edge=0.04)
    assert r["n_picks"] == 0
