"""Price a Taiwan line from an international reference line (pure math, no I/O).

The reference two-way price is de-vigged, the reference line and probability give the implied mean of the
home margin (spread) or the game total, and that mean ~ Normal(mu, sigma) prices the Taiwan line. Integer
lines push on an exact landing (±0.5 continuity correction). Design: docs/superpowers/specs/2026-10-29-tw-snapshot-design.md.
"""
from __future__ import annotations

from statistics import NormalDist
from typing import Tuple

_N = NormalDist()
_HIGH = {"spread": "home", "total": "over"}       # the side that wins when the outcome lands above the threshold
_LOW = {"spread": "away", "total": "under"}


def devig(dec_a: float, dec_b: float) -> float:
    """Multiplicative de-vig: probability of side a."""
    ia, ib = 1.0 / float(dec_a), 1.0 / float(dec_b)
    return ia / (ia + ib)


def implied_mean(ref_line: float, p_ref: float, sigma: float, kind: str) -> float:
    """spread: mean home margin from the home handicap ref_line and p(home covers);
    total: mean total from the total ref_line and p(over)."""
    z = _N.inv_cdf(float(p_ref))
    if kind == "spread":
        return -float(ref_line) + sigma * z
    if kind == "total":
        return float(ref_line) + sigma * z
    raise ValueError(f"unknown kind: {kind!r}")


def outcome_probs(line: float, mu: float, sigma: float, kind: str, side: str) -> Tuple[float, float, float]:
    """(p_win, p_push, p_loss) of one side at a Taiwan line (home handicap for spread, total for total)."""
    if kind not in _HIGH:
        raise ValueError(f"unknown kind: {kind!r}")
    if side not in (_HIGH[kind], _LOW[kind]):
        raise ValueError(f"side {side!r} does not belong to {kind}")
    t = -float(line) if kind == "spread" else float(line)    # outcome above t: home covers / over wins
    if abs(t - round(t)) < 1e-9:
        lo, hi = _N.cdf((t - 0.5 - mu) / sigma), _N.cdf((t + 0.5 - mu) / sigma)
        above, push = 1.0 - hi, hi - lo
    else:
        above, push = 1.0 - _N.cdf((t - mu) / sigma), 0.0
    below = 1.0 - above - push
    return (above, push, below) if side == _HIGH[kind] else (below, push, above)


def ev(dec: float, p_win: float, p_push: float) -> float:
    """Expected return per unit staked; a push returns the stake."""
    return p_win * float(dec) + p_push - 1.0
