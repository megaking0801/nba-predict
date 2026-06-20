"""Golden no-future-leakage test: the feature vector for any game G must be
identical whether the builder saw data ending at G or the full dataset.
This single property would have caught the old system's worst bugs.
"""
import datetime as dt
import random

import pytest

from jobs.features import FeatureBuilder, build_feature_table, norm_player_name
from jobs.teams import CANONICAL_ABBRS, normalize_espn_abbr

TEAMS4 = ["BOS", "LAL", "GSW", "WAS"]


def _player_rows(rng, team_abbr, base_quality):
    rows = []
    for i in range(9):
        minutes = max(6.0, rng.gauss(26, 6)) if i < 8 else 4.0
        pts = max(0, rng.gauss(12 + base_quality, 5))
        rows.append({
            "player_id": hash(team_abbr) % 10_000 + i,
            "team_abbr": team_abbr,
            "player_name": f"{team_abbr} Player{i}",
            "min_played": minutes,
            "pts": pts, "fgm": pts * 0.4, "fga": pts * 0.85,
            "ftm": pts * 0.15, "fta": pts * 0.2,
            "oreb": rng.uniform(0, 3), "dreb": rng.uniform(1, 6),
            "ast": rng.uniform(0, 6), "stl": rng.uniform(0, 2),
            "blk": rng.uniform(0, 1.5), "tov": rng.uniform(0, 3),
            "pf": rng.uniform(0, 4),
        })
    return rows


def _team_stats(rng, pts):
    fga = rng.gauss(88, 5)
    return {
        "pts": pts, "fgm": pts * 0.45, "fga": fga,
        "fg3m": rng.gauss(12, 3), "fg3a": rng.gauss(34, 5),
        "ftm": rng.gauss(16, 4), "fta": rng.gauss(21, 5),
        "oreb": rng.gauss(10, 3), "dreb": rng.gauss(33, 4),
        "ast": rng.gauss(25, 4), "stl": rng.gauss(7, 2),
        "blk": rng.gauss(5, 2), "tov": rng.gauss(14, 3), "pf": rng.gauss(19, 3),
    }


def make_bundles(n_games=60, seed=7):
    rng = random.Random(seed)
    bundles = []
    date = dt.date(2024, 10, 22)
    season = "2024-25"
    for i in range(n_games):
        home, away = rng.sample(TEAMS4, 2)
        h_pts = int(max(80, rng.gauss(114, 11)))
        a_pts = int(max(80, rng.gauss(111, 11)))
        if h_pts == a_pts:
            h_pts += 1
        bundles.append({
            "game_id": f"00224{i:05d}", "season": season, "season_type": "regular",
            "date": date, "home_abbr": home, "away_abbr": away,
            "margin": h_pts - a_pts,
            "team_stats": {home: {**_team_stats(rng, h_pts), "team_abbr": home},
                           away: {**_team_stats(rng, a_pts), "team_abbr": away}},
            "player_stats": (_player_rows(rng, home, 2) + _player_rows(rng, away, 0)),
        })
        if i % 2 == 1:
            date += dt.timedelta(days=1)
    return bundles


def test_point_in_time_golden():
    """Feature row i from the full table == emit() from a builder fed only
    bundles[:i]."""
    bundles = make_bundles()
    table = build_feature_table(bundles)
    # "total" is a label (like "margin"); everything else in the table is a
    # point-in-time feature — including the totals features from emit_totals().
    feature_cols = [c for c in table.columns
                    if c not in ("game_id", "season", "season_type", "date",
                                 "home_abbr", "away_abbr", "margin", "total",
                                 "eligible")]
    for i in (0, 1, 15, 37, len(bundles) - 1):
        builder = FeatureBuilder()
        for b in bundles[:i]:
            builder.update(b)
        b = bundles[i]
        feats = builder.emit(b["home_abbr"], b["away_abbr"], b["date"])
        tfeats = builder.emit_totals(b["home_abbr"], b["away_abbr"], b["date"])
        feats = {**feats, **{k: v for k, v in tfeats.items() if k not in feats}}
        row = table.iloc[i]
        for col in feature_cols:
            assert feats[col] == row[col], (
                f"game {i} feature {col}: prefix={feats[col]} full={row[col]}")


def test_determinism():
    t1 = build_feature_table(make_bundles())
    t2 = build_feature_table(make_bundles())
    assert t1.equals(t2)


def test_unknown_abbr_raises():
    builder = FeatureBuilder()
    with pytest.raises(ValueError):
        builder.emit("WSH", "BOS", dt.date(2024, 11, 1))  # ESPN variant, not canonical
    with pytest.raises(ValueError):
        builder.emit("XXX", "BOS", dt.date(2024, 11, 1))


def test_all_30_espn_variants_normalize():
    for variant, expected in [("GS", "GSW"), ("NO", "NOP"), ("NY", "NYK"),
                              ("SA", "SAS"), ("UTAH", "UTA"), ("WSH", "WAS")]:
        assert normalize_espn_abbr(variant) == expected
    for abbr in CANONICAL_ABBRS:
        assert normalize_espn_abbr(abbr) == abbr
    assert len(CANONICAL_ABBRS) == 30


def test_name_normalization():
    assert norm_player_name("Luka Dončić") == "luka doncic"
    assert norm_player_name("Jaren Jackson Jr.") == "jaren jackson"
    assert norm_player_name("O.G. Anunoby") == "o g anunoby"
