"""C1 subset-bias check: point-in-time context, subset definitions, residual signs, the pass rule."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from jobs import market_bias as mb

D = dt.date


def _b(gid, home, away, date, margin, season="2018-19", stype="regular", players=None, neutral=None,
       hs=110, as_=100):
    return {"game_id": gid, "season": season, "season_type": stype, "date": date, "home_abbr": home,
            "away_abbr": away, "margin": margin, "home_score": hs, "away_score": as_, "neutral_site": neutral,
            "player_stats": players or []}


def _p(team, pid, minutes):
    return {"team_abbr": team, "player_id": pid, "min_played": minutes}


# ---------- context ----------

def test_b2b_rest_streak_and_last_margin_use_only_earlier_games():
    games = [_b("1", "BOS", "LAL", D(2019, 1, 1), 25), _b("2", "LAL", "BOS", D(2019, 1, 2), 3),
             _b("3", "BOS", "MIA", D(2019, 1, 6), -4)]
    c = mb.context_rows(games).set_index("game_id")
    assert not c.loc["1", "b2b_home"] and c.loc["1", "streak_home"] == 0
    assert c.loc["2", "b2b_home"] and c.loc["2", "b2b_away"]                  # both played on 01-01
    assert c.loc["2", "last_margin_home"] == -25 and c.loc["2", "last_margin_away"] == 25
    assert c.loc["2", "streak_away"] == 1 and c.loc["2", "streak_home"] == -1
    assert c.loc["3", "rest_home"] == 3 and c.loc["3", "streak_home"] == -1     # BOS lost game 2
    assert c.loc["3", "rest_away"] == 3 and c.loc["3", "gp_home"] == 2


def test_travel_and_time_zone_for_the_away_team():
    games = [_b("1", "LAL", "BOS", D(2019, 1, 1), 5), _b("2", "MIA", "LAL", D(2019, 1, 3), 5)]
    c = mb.context_rows(games).set_index("game_id")
    assert pd.isna(c.loc["1", "travel_away_km"])                               # no earlier venue
    assert 3700 < c.loc["2", "travel_away_km"] < 3900                          # LAL -> MIA
    assert c.loc["2", "tz_shift_away"] == 3


def test_seasons_reset_state_and_preseason_is_ignored():
    games = [_b("p", "BOS", "LAL", D(2018, 10, 1), 30, stype="preseason"),
             _b("1", "BOS", "LAL", D(2019, 4, 1), 30), _b("2", "BOS", "LAL", D(2019, 10, 20), 1, season="2019-20")]
    c = mb.context_rows(games).set_index("game_id")
    assert "p" not in c.index
    assert c.loc["2", "gp_home"] == 0 and c.loc["2", "streak_home"] == 0 and pd.isna(c.loc["2", "last_margin_home"])


def test_star_out_previous_game_uses_season_minutes_and_skips_placeholder_boxes():
    star = [_p("BOS", 1, 38), _p("BOS", 2, 36), _p("BOS", 3, 20)]
    games = [_b(str(i), "BOS", "LAL", D(2019, 1, 1) + dt.timedelta(days=2 * i), 5, players=star) for i in range(5)]
    games.append(_b("5", "BOS", "LAL", D(2019, 1, 11), 5, players=[_p("BOS", 2, 40), _p("BOS", 3, 30)]))   # #1 sits
    games.append(_b("6", "BOS", "LAL", D(2019, 1, 13), 5, players=star))
    games.append(_b("7", "BOS", "LAL", D(2019, 1, 15), 5, players=[]))        # placeholder box
    games.append(_b("8", "BOS", "LAL", D(2019, 1, 17), 5, players=star))
    c = mb.context_rows(games).set_index("game_id")
    assert not c.loc["5", "star_out_prev_home"]                               # star played the game before
    assert c.loc["6", "star_out_prev_home"]                                   # star missed game 5
    assert not c.loc["8", "star_out_prev_home"]                               # previous box was empty: no claim


def test_left_games_come_from_the_public_schedule():
    games = [_b(str(i), "BOS", "LAL", D(2019, 1, 1) + dt.timedelta(days=2 * i), 5) for i in range(12)]
    c = mb.context_rows(games).set_index("game_id")
    assert c.loc["0", "left_home"] == 12 and c.loc["11", "left_home"] == 1


# ---------- subsets and residuals ----------

def _frame(n=2000, seed=0, bias=None):
    """Synthetic games with neutral context; `bias` = (column, value) adds residual to rows where column is True."""
    rng = np.random.default_rng(seed)
    dates = [D(2015, 11, 1) + dt.timedelta(days=int(i * 3600 / n)) for i in range(n)]
    def season(d):
        y = d.year if d.month >= 8 else d.year - 1
        return f"{y}-{str(y + 1)[2:]}"
    hs = np.round(rng.normal(-2, 6, n) * 2) / 2
    df = pd.DataFrame({"game_id": [f"g{i}" for i in range(n)], "season": [season(d) for d in dates],
                       "season_type": "regular", "date": dates, "home_abbr": "BOS", "away_abbr": "LAL",
                       "neutral": False, "b2b_home": False, "b2b_away": rng.random(n) < 0.15, "rest_home": 1, "rest_away": 1,
                       "streak_home": 0, "streak_away": 0, "last_margin_home": 0.0, "last_margin_away": 0.0,
                       "gp_home": 30, "gp_away": 30, "winpct_home": 0.5, "winpct_away": 0.5, "left_home": 40, "left_away": 40,
                       "travel_away_km": 500.0, "tz_shift_away": 0.0, "star_out_prev_home": False, "star_out_prev_away": False})
    resid = rng.normal(0, 12, n)
    if bias:
        col, val = bias
        resid = resid + np.where(df[col], val, 0.0)
    df["margin"] = -hs + resid
    df["total_line"] = 220.0
    df["total"] = 220.0 + rng.normal(0, 18, n)
    lines = pd.DataFrame({"game_id": df["game_id"], "home_spread": hs, "total_line": df.pop("total_line")})
    return df, lines


def test_the_registered_subset_list_is_fixed():
    df, lines = _frame(200)
    names = [s["name"] for s in mb.subsets(df.merge(lines, on="game_id"))]
    assert len(names) == 24 and len(set(names)) == 24
    assert {"key_number_3_or_7", "around_key_numbers", "star_out_prev_game", "home_underdog"} <= set(names)


def test_away_focus_flips_the_residual_sign():
    df, lines = _frame(200)
    m = df.merge(lines, on="game_id")
    s = {x["name"]: x for x in mb.subsets(m)}["away_b2b"]
    r = (m["margin"] + m["home_spread"]) * s["sign"]
    assert np.allclose(r[s["mask"]], -(m["margin"] + m["home_spread"])[s["mask"]])


def test_push_counts_as_zero_and_is_left_out_of_the_cover_rate():
    r = pd.Series([3.0, 0.0, -1.0, 0.0] * 10)
    t = mb.test_subset(r, pd.Series([D(2019, 1, 1)] * 40), pd.Series(["2018-19"] * 40), 0.99, 50, 1)
    assert t["n"] == 40 and t["n_push"] == 20 and t["mean_resid"] == pytest.approx(0.5) and t["cover_rate"] == 0.5


def test_thresholds():
    assert mb.thresholds("spread", None) == {"intl": 0.75, "taiwan": 2.24}
    t = mb.thresholds("total", 18.0)
    assert 1.0 < t["intl"] < 1.2 and 3.0 < t["taiwan"] < 3.4


def test_a_planted_bias_is_found_and_replicates():
    df, lines = _frame(9000, seed=1, bias=("b2b_away", -3.0))     # away teams on a b2b lose by 3 more than the line
    out = mb.run(df, lines, reps=200, seed=1)
    s = out["subsets"]["away_b2b"]
    assert s["explore"]["passes_explore"] and s["explore"]["mean_resid"] > 2.0
    assert "away_b2b" in out["replicated"] and out["subsets"]["away_b2b"]["explore"]["pays_taiwan_explore"]


def test_no_bias_finds_nothing_and_never_touches_the_confirmation_segment():
    df, lines = _frame(9000, seed=2)
    out = mb.run(df, lines, reps=200, seed=1)
    assert out["replicated"] == []
    for v in out["subsets"].values():
        for part in ("explore", "validate"):
            seasons = v.get(part, {}).get("per_season", {})
            assert all(s < "2025-26" for s in seasons)


def test_module_is_read_only():
    src = Path(mb.__file__).read_text(encoding="utf-8")
    for word in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "commit()", "executemany", "ensure_schema"):
        assert word not in src
