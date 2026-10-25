"""Tests for jobs.quality.checks_box (B checks)."""
from __future__ import annotations

from pathlib import Path

from jobs.quality import checks_box as cb
from jobs.quality.core import Scope
from tests.quality_fakes import assert_select_only, fake_run, frame

ALL = Scope(None, None)
COLS = ["season", "season_type", "n_total", "n_bad"]


def _run(key, rows, cols=COLS):
    run, db = fake_run({key: frame(rows, cols)})
    return run, db


def test_b1_b2_count_missing_team_and_placeholder_rows():
    run, _ = _run("qc:B1", [("2015-16", "regular", 2460, 322)])
    got = cb.check_b1_team_box_complete(run, ALL)
    assert got[0].severity == "warn" and got[0].n_bad == 322 and got[0].n_total == 2460
    run, _ = _run("qc:B2", [("2015-16", "regular", 2460, 322)])
    assert cb.check_b2_player_box_not_placeholder(run, ALL)[0].n_bad == 322


def test_b1_sql_counts_a_missing_team_row_as_bad():
    run, db = _run("qc:B1", [])
    cb.check_b1_team_box_complete(run, ALL)
    sql = db.calls[0][0]
    assert "2*count(DISTINCT g.game_id)" in sql.replace(" ", "")  or "2 * count(DISTINCT g.game_id)" in sql
    assert "LEFT JOIN public.team_game_stats" in sql


def test_b3_only_counts_teams_with_a_real_player_box():
    run, db = _run("qc:B3", [("2018-19", "regular", 2700, 23, 18)], COLS + ["off_gt_3"])
    got = cb.check_b3_player_points_match_team(run, ALL)
    assert got[0].n_bad == 23 and got[0].detail == {"off_gt_3": 18}
    assert "n_min > 0" in db.calls[0][0]            # placeholder team-games are excluded up front


def test_b4_minutes_rule_boundaries():
    f = cb.minutes_ok
    assert all(f(m) for m in (240, 265, 243, 237, 290, 315, 341))   # 341 = four overtimes, plausible
    assert not f(244) and not f(236) and not f(180) and not f(344)
    assert not f(120)        # k is clamped at 0: 120 is not 240-ish
    run, db = _run("qc:B4", [("2025-26", "regular", 2774, 23)])
    assert cb.check_b4_team_minutes_240(run, ALL)[0].severity == "warn"
    assert "240" in db.calls[0][0] and "25" in db.calls[0][0]


def test_b5_b6_are_errors():
    run, _ = _run("qc:B5", [("2015-16", "regular", 33000, 1)])
    got = cb.check_b5_impossible_lines(run, ALL)
    assert got[0].severity == "error" and got[0].n_bad == 1
    run, _ = _run("qc:B6", [("2015-16", "regular", 2460, 0)])
    assert cb.check_b6_team_points_equal_score(run, ALL)[0].severity == "error"


def test_b5_sql_covers_every_impossible_condition():
    run, db = _run("qc:B5", [])
    cb.check_b5_impossible_lines(run, ALL)
    sql = db.calls[0][0]
    for needle in ("fgm > p.fga", "ftm > p.fta", "fg3m > p.fg3a", "p.pts < 0", "min_played > 60"):
        assert needle in sql


def test_b6_sql_compares_with_the_right_side_of_the_score():
    run, db = _run("qc:B6", [])
    cb.check_b6_team_points_equal_score(run, ALL)
    sql = db.calls[0][0]
    assert "t.is_home" in sql and "g.home_score" in sql and "g.away_score" in sql


def test_empty_data_is_zero_not_an_error():
    for fn, key in ((cb.check_b1_team_box_complete, "qc:B1"), (cb.check_b2_player_box_not_placeholder, "qc:B2"),
                    (cb.check_b4_team_minutes_240, "qc:B4"), (cb.check_b5_impossible_lines, "qc:B5"),
                    (cb.check_b6_team_points_equal_score, "qc:B6")):
        run, _ = _run(key, [])
        got = fn(run, ALL)
        assert len(got) == 1 and got[0].n_bad == 0 and got[0].n_total == 0
    run, _ = _run("qc:B3", [], COLS + ["off_gt_3"])
    assert cb.check_b3_player_points_match_team(run, ALL)[0].n_total == 0


def test_all_sql_is_select_only():
    assert_select_only(Path(cb.__file__))


def test_b6_counts_a_null_team_score_as_bad():
    run, db = _run("qc:B6", [])
    cb.check_b6_team_points_equal_score(run, ALL)
    assert "IS DISTINCT FROM" in db.calls[0][0]
