"""Probability-head math: push handling, home/away symmetry, de-vig sanity."""
import math

import pytest

from jobs.model import apply_calibrator, devig, expected_value, margin_to_cover_prob


def test_integer_spread_has_push_mass():
    r = margin_to_cover_prob(mu=3.0, sigma=12.0, spread=-3.0)
    assert r["p_push"] > 0
    assert math.isclose(r["p_win"] + r["p_push"] + r["p_loss"], 1.0, abs_tol=1e-9)


def test_half_point_spread_has_no_push():
    r = margin_to_cover_prob(mu=3.0, sigma=12.0, spread=-3.5)
    assert r["p_push"] == 0.0
    assert math.isclose(r["p_win"] + r["p_loss"], 1.0, abs_tol=1e-9)


@pytest.mark.parametrize("mu,spread", [(3.0, -3.0), (3.0, -3.5), (-7.2, 6.0),
                                       (0.0, 0.0), (15.0, -12.5)])
def test_home_away_symmetry(mu, spread):
    """p_raw(home) + p_raw(away) == 1 for the same line."""
    home = margin_to_cover_prob(mu, 12.0, spread)
    away = margin_to_cover_prob(-mu, 12.0, -spread)
    assert math.isclose(home["p_raw"] + away["p_raw"], 1.0, abs_tol=1e-9)
    assert math.isclose(home["p_push"], away["p_push"], abs_tol=1e-9)


def test_monotonic_in_mu_and_spread():
    base = margin_to_cover_prob(0.0, 12.0, -3.5)["p_raw"]
    assert margin_to_cover_prob(2.0, 12.0, -3.5)["p_raw"] > base
    assert margin_to_cover_prob(0.0, 12.0, -2.5)["p_raw"] > base  # easier line


def test_fair_favorite_at_own_line():
    """If the model's margin equals the (half-point) line, cover is a coin flip."""
    r = margin_to_cover_prob(mu=5.5, sigma=12.0, spread=-5.5)
    assert math.isclose(r["p_raw"], 0.5, abs_tol=1e-9)


def test_devig_sums_to_one_and_strips_vig():
    fair_h, fair_a = devig(1.91, 1.91)
    assert math.isclose(fair_h + fair_a, 1.0, abs_tol=1e-12)
    assert math.isclose(fair_h, 0.5, abs_tol=1e-12)
    assert fair_h < 1 / 1.91  # vig removed


def test_expected_value():
    assert math.isclose(expected_value(0.55, 0.45, 1.91), 0.55 * 0.91 - 0.45, abs_tol=1e-12)


def test_calibrator_identity_and_neutral_platt():
    assert apply_calibrator({"type": "identity"}, 0.6) == 0.6
    assert apply_calibrator(None, 0.6) == 0.6
    neutral = apply_calibrator({"type": "platt", "a": 1.0, "b": 0.0}, 0.6)
    assert math.isclose(neutral, 0.6, abs_tol=1e-6)
