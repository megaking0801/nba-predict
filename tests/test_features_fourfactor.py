"""P4.2: four factors split into own-quality off/def columns, plus three-point rate."""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt

import pytest

from jobs.config import CONFIG
from jobs.features import FEATURE_GROUPS, FEATURE_NAMES, FeatureBuilder, build_feature_table, ctx_from_bundle
from tests.test_features_point_in_time import make_bundles

FACTORS = ("efg", "tov", "orb", "ftr")


def test_groups_and_the_columns_they_replace():
    assert FEATURE_GROUPS["ff"] == [f"{f}_{k}_diff" for f in FACTORS for k in ("off", "def")]
    assert FEATURE_GROUPS["three_rate"] == ["three_rate_off_diff", "three_rate_def_diff"]
    assert all(f"{f}10_diff" in FEATURE_NAMES for f in FACTORS)


def test_off_plus_def_equals_the_existing_net_diff():
    table = build_feature_table(make_bundles(n_games=60))
    for f in FACTORS:
        total = table[f"{f}_off_diff"] + table[f"{f}_def_diff"]
        assert (total - table[f"{f}10_diff"]).abs().max() < 1e-9, f


def test_heavier_defence_shrinkage_only_touches_the_defence_columns():
    base = build_feature_table(make_bundles(n_games=60))
    heavy = build_feature_table(make_bundles(n_games=60), dataclasses.replace(CONFIG, SHRINK_K_DEF=50))
    for f in FACTORS:
        assert (base[f"{f}_off_diff"] - heavy[f"{f}_off_diff"]).abs().max() < 1e-12
        assert heavy[f"{f}_def_diff"].abs().mean() < base[f"{f}_def_diff"].abs().mean()
    assert (base["efg10_diff"] == heavy["efg10_diff"]).all()          # fs1 columns do not move


def _team(fgm, fga, fg3m, fg3a, ftm, fta, oreb, dreb, tov, pts):
    return dict(fgm=fgm, fga=fga, fg3m=fg3m, fg3a=fg3a, ftm=ftm, fta=fta, oreb=oreb, dreb=dreb,
                tov=tov, pts=pts, ast=20, stl=7, blk=5, pf=18)


def _game(home, away, date, home_stats, away_stats, margin=3):
    return {"game_id": f"{home}{away}{date}", "season": "2024-25", "season_type": "regular", "date": date,
            "home_abbr": home, "away_abbr": away, "margin": margin,
            "team_stats": {home: {**home_stats, "team_abbr": home}, away: {**away_stats, "team_abbr": away}},
            "player_stats": []}


def test_three_rate_columns_by_hand():
    bos = _team(40, 80, 12, 40, 15, 20, 10, 30, 12, 107)       # three-point rate 0.5
    lal = _team(38, 80, 8, 24, 15, 20, 10, 30, 12, 99)         # three-point rate 0.3
    b = FeatureBuilder()
    b.update(_game("BOS", "LAL", dt.date(2025, 1, 1), bos, lal))
    row = b.emit("BOS", "LAL", dt.date(2025, 1, 3), ctx_from_bundle({}))
    w = 1 / (1 + CONFIG.SHRINK_K_SHORT)
    assert row["three_rate_off_diff"] == pytest.approx(w * (0.5 - 0.3))
    assert row["three_rate_def_diff"] == pytest.approx(w * (0.5 - 0.3))     # LAL allowed 0.5, BOS allowed 0.3


def test_games_without_a_box_score_do_not_move_the_split_columns():
    bundles = make_bundles(n_games=20)
    with_gap = copy.deepcopy(bundles)
    with_gap[8]["team_stats"] = {}
    clean = build_feature_table([b for i, b in enumerate(bundles) if i != 8])
    gap = build_feature_table(with_gap)
    cols = FEATURE_GROUPS["ff"] + FEATURE_GROUPS["three_rate"]
    diff = gap.loc[9:, cols].to_numpy() - clean.loc[8:, cols].to_numpy()
    assert abs(diff).max() < 1e-12
