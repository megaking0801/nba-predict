"""jobs.tw_price: de-vig, reference line -> mean, push-aware outcome probabilities and EV."""
from statistics import NormalDist

import pytest

from jobs import jbot
from jobs import tw_price as pr


def test_devig_even():
    assert pr.devig(1.909, 1.909) == pytest.approx(0.5)
    assert pr.devig(1.5, 3.0) == pytest.approx((1 / 1.5) / (1 / 1.5 + 1 / 3.0))


def test_matches_jbot_at_minus_110():
    p = pr.devig(1.909, 1.909)
    mu = pr.implied_mean(-5.5, p, 12.5, "spread")
    w, push, _ = pr.outcome_probs(-4.5, mu, 12.5, "spread", "home")
    assert push == 0.0
    assert pr.ev(1.72, w, push) == pytest.approx(jbot.spread_ev(-4.5, 1.72, 1.72, -5.5)["ev_home"], abs=1e-9)
    w, push, _ = pr.outcome_probs(-4.5, mu, 12.5, "spread", "away")
    assert pr.ev(1.72, w, push) == pytest.approx(jbot.spread_ev(-4.5, 1.72, 1.72, -5.5)["ev_away"], abs=1e-9)
    mu_t = pr.implied_mean(230.5, p, 19.0, "total")
    w, push, _ = pr.outcome_probs(232.5, mu_t, 19.0, "total", "over")
    assert pr.ev(1.72, w, push) == pytest.approx(jbot.total_ev(232.5, 1.72, 1.72, 230.5)["ev_over"], abs=1e-9)


def test_implied_mean_moves_with_the_price():
    # home a bigger favourite than -5.5 at even money when its cover is priced above 50%
    assert pr.implied_mean(-5.5, 0.55, 12.5, "spread") > 5.5
    assert pr.implied_mean(230.5, 0.45, 19.0, "total") < 230.5


def test_integer_line_has_push():
    w, push, loss = pr.outcome_probs(-5.0, mu=5.0, sigma=12.5, kind="spread", side="home")
    nd = NormalDist()
    assert push == pytest.approx(nd.cdf(0.04) - nd.cdf(-0.04), abs=1e-12)
    assert push == pytest.approx(0.0319, abs=1e-4)
    assert w + push + loss == pytest.approx(1.0)
    assert w == pytest.approx(loss)
    assert pr.ev(2.0, w, push) == pytest.approx(2.0 * w + push - 1.0)


def test_away_and_under_are_complements():
    for kind, line, mu, a, b in [("spread", -4.5, 6.0, "home", "away"), ("total", 231.5, 228.0, "over", "under")]:
        wa, pa, la = pr.outcome_probs(line, mu, 15.0, kind, a)
        wb, pb, lb = pr.outcome_probs(line, mu, 15.0, kind, b)
        assert pa == pb == 0.0
        assert wa == pytest.approx(lb) and wb == pytest.approx(la)
        assert wa + wb == pytest.approx(1.0)


def test_bad_side_raises():
    with pytest.raises(ValueError):
        pr.outcome_probs(-4.5, 5.0, 12.5, "spread", "over")
