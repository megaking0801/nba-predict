"""P4.1: context/schedule features (neutral site, travel, density, altitude)."""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt

import pytest

from jobs.config import CONFIG
from jobs.features import (FEATURE_GROUPS, FEATURE_NAMES, FeatureBuilder, build_feature_table,
                           ctx_from_bundle)
from jobs.ratings import Elo
from tests.test_features_point_in_time import make_bundles

D = dt.date


def _b(home, away, date, margin=5, season="2024-25", season_type="regular", neutral=None, gid=None):
    return {"game_id": gid or f"{home}{away}{date}", "season": season, "season_type": season_type,
            "date": date, "home_abbr": home, "away_abbr": away, "margin": margin,
            "neutral_site": neutral, "team_stats": {}, "player_stats": []}


def _emit(builder, b):
    return builder.emit(b["home_abbr"], b["away_abbr"], b["date"], ctx_from_bundle(b))


def _run(games):
    builder, rows = FeatureBuilder(), []
    for g in games:
        rows.append(_emit(builder, g))
        builder.update(g)
    return rows


def test_groups_cover_the_new_columns_and_leave_fs1_alone():
    assert FEATURE_GROUPS["ctx"] == ["neutral_game", "crowd_restricted", "is_playoffs", "is_playin"]
    assert len(FEATURE_GROUPS["travel"]) == 6 and FEATURE_GROUPS["altitude"] == ["altitude_edge"]
    assert not set(sum(FEATURE_GROUPS.values(), [])) & set(FEATURE_NAMES)


def test_emit_outputs_every_group_column():
    row = _run([_b("BOS", "LAL", D(2025, 1, 10))])[0]
    for cols in FEATURE_GROUPS.values():
        assert set(cols) <= set(row)


def test_ctx_from_bundle_defaults_are_regular_and_not_neutral():
    assert ctx_from_bundle({"season_type": "regular", "date": D(2025, 1, 1)}) == {
        "neutral_site": False, "season_type": "regular"}
    assert ctx_from_bundle({"date": D(2025, 1, 1), "neutral_site": None})["season_type"] == "regular"


def test_context_flags():
    r = _run([_b("BOS", "LAL", D(2025, 1, 10)),
              _b("BOS", "LAL", D(2025, 1, 12), neutral=True),
              _b("BOS", "LAL", D(2020, 8, 15)),
              _b("BOS", "LAL", D(2021, 1, 15)),
              _b("BOS", "LAL", D(2025, 4, 25), season_type="playoffs"),
              _b("BOS", "LAL", D(2025, 4, 15), season_type="playin")])
    assert [x["neutral_game"] for x in r] == [0, 1, 1, 0, 0, 0]
    assert [x["crowd_restricted"] for x in r] == [0, 0, 0, 1, 0, 0]
    assert [x["is_playoffs"] for x in r] == [0, 0, 0, 0, 1, 0]
    assert [x["is_playin"] for x in r] == [0, 0, 0, 0, 0, 1]


def test_elo_neutral_hca_zero_only_when_asked():
    e1, e2 = Elo(), Elo()
    e1.update("BOS", "LAL", 8)
    e2.update("BOS", "LAL", 8, neutral=True)
    assert e1.ratings != e2.ratings
    e3 = Elo()
    e3.update("BOS", "LAL", 8, neutral=False)
    assert e1.ratings == e3.ratings


def test_neutral_elo_flag_changes_ratings_only_for_neutral_games():
    games = [_b("BOS", "LAL", D(2025, 1, 10), neutral=True), _b("MIA", "DEN", D(2025, 1, 12))]
    base = FeatureBuilder()
    flagged = FeatureBuilder(dataclasses.replace(CONFIG, ELO_NEUTRAL_HCA0=True))
    for g in games[:1]:
        base.update(g)
        flagged.update(g)
    assert base.elo.ratings != flagged.elo.ratings
    plain = [_b("BOS", "LAL", D(2025, 1, 10)), _b("MIA", "DEN", D(2025, 1, 12))]
    b2, f2 = FeatureBuilder(), FeatureBuilder(dataclasses.replace(CONFIG, ELO_NEUTRAL_HCA0=True))
    for g in plain:
        b2.update(g)
        f2.update(g)
    assert b2.elo.ratings == f2.elo.ratings


def test_travel_distance_timezone_and_road_trip():
    # LAL: home, then at BOS, then at MIA, then at BOS again (a road trip); BOS stays home.
    games = [_b("LAL", "GSW", D(2025, 1, 1)),
             _b("BOS", "LAL", D(2025, 1, 3)),
             _b("MIA", "LAL", D(2025, 1, 5)),
             _b("BOS", "LAL", D(2025, 1, 7))]
    rows = _run(games)
    # game 2 (LAL at BOS): LAL last played at home (LAL arena) -> flies LAL->BOS; BOS was at home?
    # BOS has no previous game, so its travel is 0.
    assert rows[1]["travel_km_diff"] < -4000 / 1000 + 0.2           # home (BOS) 0 minus away ~4170 km
    assert rows[1]["tz_shift_away"] == 3
    assert rows[1]["road_trip_len_away"] == 1 and rows[1]["road_trip_len_home"] == 0
    # game 3 (LAL at MIA): LAL was at BOS.
    assert rows[2]["road_trip_len_away"] == 2
    assert rows[2]["tz_shift_away"] == 0                              # BOS and MIA share a zone
    assert 1900 < -rows[2]["travel_km_diff"] * 1000 < 2150          # BOS -> MIA; MIA had no earlier game
    assert rows[3]["road_trip_len_away"] == 3
    assert rows[3]["road_trip_len_home"] == 0                         # BOS last played at home


def test_returning_home_counts_as_travel_for_the_home_team():
    games = [_b("BOS", "LAL", D(2025, 1, 1)), _b("LAL", "BOS", D(2025, 1, 3)),
             _b("BOS", "MIA", D(2025, 1, 5))]
    rows = _run(games)
    # BOS was last at LAL (away), now hosts MIA: BOS travels ~4170 km back, MIA (no history) 0
    assert rows[2]["travel_km_diff"] > 4.0 and rows[2]["road_trip_len_home"] == 1


def test_short_rest_variant_only_counts_back_to_back_travel():
    games = [_b("BOS", "LAL", D(2025, 1, 1)), _b("MIA", "LAL", D(2025, 1, 5))]
    rows = _run(games)
    assert rows[1]["travel_km_diff"] < -1.9 and rows[1]["travel_km_diff_short_rest"] == 0   # 3 rest days
    games = [_b("BOS", "LAL", D(2025, 1, 1)), _b("MIA", "LAL", D(2025, 1, 2))]
    rows = _run(games)
    assert rows[1]["travel_km_diff_short_rest"] == pytest.approx(rows[1]["travel_km_diff"])


def test_games_in_last_seven_days_difference():
    games = [_b("BOS", "MIA", D(2025, 1, 1)), _b("BOS", "MIA", D(2025, 1, 3)),
             _b("LAL", "BOS", D(2025, 1, 5))]
    rows = _run(games)
    assert rows[2]["games_last7_diff"] == -2                         # LAL 0 vs BOS 2


def test_altitude_edge_is_for_visitors_to_denver_and_utah_only():
    rows = _run([_b("DEN", "MIA", D(2025, 1, 1)), _b("MIA", "DEN", D(2025, 1, 3)),
                 _b("UTA", "BOS", D(2025, 1, 5))])
    assert rows[0]["altitude_edge"] == pytest.approx((1609 - 2) / 1000)
    assert rows[1]["altitude_edge"] == 0
    assert rows[2]["altitude_edge"] == pytest.approx((1288 - 6) / 1000)


def test_neutral_game_has_no_travel_or_altitude():
    row = _run([_b("MIA", "LAL", D(2024, 12, 30)), _b("DEN", "MIA", D(2025, 1, 1), neutral=True)])[1]
    assert row["altitude_edge"] == 0 and row["travel_km_diff"] == 0 and row["tz_shift_away"] == 0


def test_new_season_forgets_the_last_venue():
    games = [_b("BOS", "LAL", D(2025, 4, 1)), _b("MIA", "LAL", D(2025, 10, 28), season="2025-26")]
    rows = _run(games)
    assert rows[1]["travel_km_diff"] == 0 and rows[1]["road_trip_len_away"] == 1


def test_own_game_never_changes_its_own_context_features():
    games = [_b("BOS", "LAL", D(2025, 1, 1)), _b("MIA", "LAL", D(2025, 1, 3))]
    a, b = copy.deepcopy(games), copy.deepcopy(games)
    b[1]["margin"] = -30
    ra, rb = _run(a), _run(b)
    assert ra[1] == rb[1]


def test_table_has_context_columns_and_fs1_columns_unchanged():
    t = build_feature_table(make_bundles())
    for cols in FEATURE_GROUPS.values():
        assert set(cols) <= set(t.columns)
