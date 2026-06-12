"""Edge/EV/abstain/selection logic, shared by predict_daily and the app.

Fixes the legacy system's two pick-layer errors: edges are measured against
de-vigged two-sided fair probabilities (not 1/odds, which bakes in ~2.3% of
phantom vig), and there is no filler — fewer than MAX_PICKS_PER_DAY
qualifying games means fewer picks, never sub-threshold or line-less ones.
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

from jobs.config import CONFIG
from jobs.model import apply_calibrator, devig, expected_value, margin_to_cover_prob


def decide_game(mu: float, sigma: float, *,
                home_spread: Optional[float],
                home_price: Optional[float],
                away_price: Optional[float],
                calibrator: Optional[dict],
                min_edge: float,
                line_age_hours: Optional[float] = None,
                games_played_min: Optional[float] = None,
                injury_veto: bool = False,
                cfg=CONFIG) -> Dict:
    """One game's full decision. Always returns probabilities when a line
    exists (the board shows them); pick_side is set only when every guard
    passes and the edge clears the threshold."""
    out: Dict = {
        "pred_margin": float(mu),
        "p_raw": None, "p_cal": None,
        "edge_home": None, "edge_away": None,
        "ev_home": None, "ev_away": None,
        "pick_side": None, "abstain_reason": None,
        "edge_pick": None, "ev_pick": None,
    }

    if home_spread is None:
        out["abstain_reason"] = "no_line"
        return out

    pr = margin_to_cover_prob(mu, sigma, home_spread)
    p_raw = pr["p_raw"]
    p_cal = apply_calibrator(calibrator, p_raw)
    out["p_raw"], out["p_cal"] = p_raw, p_cal

    if home_price and away_price and home_price > 1.0 and away_price > 1.0:
        fair_home, fair_away = devig(home_price, away_price)
        out["edge_home"] = p_cal - fair_home
        out["edge_away"] = (1.0 - p_cal) - fair_away
        # push-aware win/loss masses, scaled to the calibrated probability
        scale = p_cal / pr["p_raw"] if pr["p_raw"] > 0 else 1.0
        p_win_h = min(1.0, pr["p_win"] * scale)
        p_loss_h = max(0.0, 1.0 - p_win_h - pr["p_push"])
        out["ev_home"] = expected_value(p_win_h, p_loss_h, home_price)
        out["ev_away"] = expected_value(p_loss_h, p_win_h, away_price)

    # guards (an abstaining game still shows its probabilities on the board)
    if line_age_hours is not None and line_age_hours > cfg.LINE_MAX_AGE_HOURS:
        out["abstain_reason"] = "stale_line"
        return out
    if games_played_min is not None and games_played_min < cfg.MIN_GP_OTHER_SEASONS:
        out["abstain_reason"] = "early_season"
        return out
    if abs(mu + home_spread) > cfg.DISAGREEMENT_GUARD_PTS:
        out["abstain_reason"] = "disagreement_guard"
        return out
    if injury_veto:
        out["abstain_reason"] = "injury_veto"
        return out
    if out["edge_home"] is None:
        out["abstain_reason"] = "no_prices"
        return out

    if out["edge_home"] >= out["edge_away"]:
        side, edge, ev = "HOME", out["edge_home"], out["ev_home"]
    else:
        side, edge, ev = "AWAY", out["edge_away"], out["ev_away"]

    if edge < min_edge or ev is None or ev <= 0:
        out["abstain_reason"] = "below_threshold"
        return out

    out["pick_side"], out["edge_pick"], out["ev_pick"] = side, edge, ev
    return out


def cap_picks_per_day(decisions: List[Dict], cfg=CONFIG) -> None:
    """In-place: keep the top MAX_PICKS_PER_DAY picks per game day by edge;
    demote the rest to abstain('capacity'). No filler in the other direction."""
    by_day: Dict[dt.date, List[Dict]] = {}
    for d in decisions:
        if d.get("pick_side"):
            by_day.setdefault(d["game_date_et"], []).append(d)
    for day, picks in by_day.items():
        picks.sort(key=lambda d: d["edge_pick"], reverse=True)
        for d in picks[cfg.MAX_PICKS_PER_DAY:]:
            d["pick_side"] = None
            d["abstain_reason"] = "capacity"
            d["edge_pick"] = d["ev_pick"] = None


def effective_min_edge(calibrator_metrics: Optional[dict], cfg=CONFIG) -> float:
    if calibrator_metrics and calibrator_metrics.get("alarm"):
        return cfg.MIN_EDGE_ALARM_MODE
    return cfg.MIN_EDGE
