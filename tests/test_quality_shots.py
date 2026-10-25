"""Tests for jobs.quality.checks_shots (D checks)."""
from __future__ import annotations

from pathlib import Path

from jobs.quality import checks_shots as cs
from jobs.quality.core import Scope
from tests.quality_fakes import assert_select_only, fake_run, frame

ALL = Scope(None, None)
D2_COLS = ["season", "season_type", "n_exact", "n_minor", "n_major", "n_none", "n_no_box"]
G = ["season", "season_type", "n_total", "n_bad"]


def test_shots_class():
    f = cs.shots_class
    assert f(None, 90) == "no_box" and f(None, None) == "no_box"
    assert f(90, None) == "none" and f(90, 0) == "none"
    assert f(90, 90) == "exact"
    assert f(90, 92) == "minor" and f(90, 88) == "minor"
    assert f(90, 93) == "major" and f(90, 87) == "major"


def test_d2_denominator_excludes_no_box():
    # 2015-16 regular as in the SQL results: 2460 team-games, 322 placeholders (no box fga)
    run, _ = fake_run({"qc:D2": frame([("2015-16", "regular", 1115, 482, 541, 0, 322)], D2_COLS)})
    f = cs.check_d2_shots_class_share(run, ALL)[0]
    assert f.n_total == 2138 and f.n_bad == 541 and f.severity == "warn"
    assert f.detail["n_comparable"] == 2138
    assert abs(f.detail["exact_pct"] - 52.2) < 0.1
    assert abs(f.detail["major_pct"] - 25.3) < 0.1
    assert abs(f.detail["minor_pct"] - 22.5) < 0.1 and f.detail["none_pct"] == 0


def test_d2_counts_none_in_the_denominator_but_not_as_exact():
    run, _ = fake_run({"qc:D2": frame([("2025-26", "playoffs", 100, 60, 0, 16, 0)], D2_COLS)})
    f = cs.check_d2_shots_class_share(run, ALL)[0]
    assert f.n_total == 176 and f.n_bad == 16
    assert abs(f.detail["exact_pct"] - 100 * 100 / 176) < 0.1 and f.detail["none_pct"] > 9


def test_d2_small_samples_are_info():
    run, _ = fake_run({"qc:D2": frame([("2026-27", "regular", 43, 28, 1, 0, 0)], D2_COLS)})
    f = cs.check_d2_shots_class_share(run, ALL)[0]
    assert f.severity == "info" and f.detail["n_comparable"] == 72


def test_d1_missing_shots_is_an_error():
    run, db = fake_run({"qc:D1": frame([("2025-26", "playoffs", 85, 8)], G)})
    f = cs.check_d1_shots_present(run, ALL)[0]
    assert f.severity == "error" and f.n_bad == 8
    assert "player_game_stats" in db.calls[0][0]      # only games that have a player box count


def test_d3_excludes_no_box_and_is_info():
    run, db = fake_run({"qc:D3": frame([("2025-26", "regular", 2462, 936)], G)})
    f = cs.check_d3_shots_3pa_match(run, ALL)[0]
    assert f.severity == "info" and f.n_bad == 936
    assert "t.fg3a IS NOT NULL" in db.calls[0][0]


def test_d4_unknown_share_is_info():
    run, _ = fake_run({"qc:D4": frame([("2025-26", "regular", 200000, 30000)], G)})
    f = cs.check_d4_shots_unknown_share(run, ALL)[0]
    assert f.severity == "info" and f.n_bad == 30000


def test_empty_data_gives_zero_findings():
    run, _ = fake_run({"qc:D2": frame([], D2_COLS)})
    f = cs.check_d2_shots_class_share(run, ALL)
    assert len(f) == 1 and f[0].n_total == 0 and f[0].n_bad == 0
    for fn, key in ((cs.check_d1_shots_present, "qc:D1"), (cs.check_d3_shots_3pa_match, "qc:D3"),
                    (cs.check_d4_shots_unknown_share, "qc:D4")):
        run, _ = fake_run({key: frame([], G)})
        assert fn(run, ALL)[0].n_total == 0


def test_d5_impossible_bucket_combinations():
    run, db = fake_run({"qc:D5": frame([("2021-22", "regular", 180000, 700)], G)})
    f = cs.check_d5_shots_impossible_combo(run, ALL)[0]
    assert f.severity == "warn" and f.n_bad == 700 and f.n_total == 180000
    sql = db.calls[0][0]
    # a two beyond 24 ft, and a three inside 14 ft (even the corner three is 22 ft out)
    assert "pts_value = 2 AND b.dist_bucket = '24+'" in sql
    assert "pts_value = 3 AND b.dist_bucket IN ('0-4', '4-8', '8-14')" in sql
    run, _ = fake_run({"qc:D5": frame([], G)})
    assert cs.check_d5_shots_impossible_combo(run, ALL)[0].n_total == 0


def test_all_sql_is_select_only():
    assert_select_only(Path(cs.__file__))
