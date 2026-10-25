"""Tests for jobs.quality.checks_games (A and C checks)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from jobs.quality import checks_games as cg
from jobs.quality.core import Scope
from tests.quality_fakes import assert_select_only, fake_run, frame

ALL = Scope(None, None)
COLS = ["season", "season_type", "n_total", "n_bad"]


def _one(check, key, rows, cols=COLS, scope=ALL):
    run, db = fake_run({key: frame(rows, cols)})
    return check(run, scope), db


def test_a2_expected_game_counts_and_cup_final():
    teams = lambda season, counts: [(season, f"T{i}", c) for i, c in enumerate(counts)]
    rows = (teams("2022-23", [82] * 30)
            + teams("2020-21", [72] * 30)
            + teams("2019-20", [64, 75, 70] * 10)              # not checked at all
            + teams("2023-24", [82] * 28 + [83, 83])            # NBA Cup final teams
            + teams("2026-27", [3] * 30))                       # season in progress: skipped
    got, _ = _one(cg.check_a2_team_game_counts, "qc:A2", rows, ["season", "team_abbr", "n"])
    by_scope = {f.scope: f for f in got}
    assert by_scope["2022-23/regular"].n_bad == 0 and by_scope["2022-23/regular"].n_total == 30
    assert by_scope["2020-21/regular"].n_bad == 0
    assert by_scope["2023-24/regular"].n_bad == 0
    assert by_scope["2023-24/regular-83"].n_bad == 0 and by_scope["2023-24/regular-83"].severity == "error"
    assert not any(s.startswith(("2019-20", "2026-27")) for s in by_scope)


def test_a2_flags_wrong_counts_and_wrong_number_of_83s():
    rows = [("2022-23", f"T{i}", 82 if i else 80) for i in range(30)]          # one team at 80
    rows += [("2024-25", f"T{i}", 83 if i < 3 else 82) for i in range(30)]     # three teams at 83
    got, _ = _one(cg.check_a2_team_game_counts, "qc:A2", rows, ["season", "team_abbr", "n"])
    by_scope = {f.scope: f for f in got}
    assert by_scope["2022-23/regular"].n_bad == 1
    assert by_scope["2024-25/regular-83"].n_bad == 1 and by_scope["2024-25/regular-83"].severity == "error"


def test_a2_skipped_in_since_mode():
    run, db = fake_run({"qc:A2": frame([], ["season", "team_abbr", "n"])})
    got = cg.check_a2_team_game_counts(run, Scope(None, date(2026, 10, 1)))
    assert got[0].n_total == 0 and not db.calls     # window runs do not query season totals


def test_a3_a5_a6_error_checks_count_bad_rows():
    for fn, key in ((cg.check_a3_duplicate_games, "qc:A3"), (cg.check_a5_stuck_not_final, "qc:A5"),
                    (cg.check_a6_final_without_score, "qc:A6")):
        ok, _ = _one(fn, key, [("2025-26", "regular", 1230, 0)])
        bad, _ = _one(fn, key, [("2025-26", "regular", 1230, 2)])
        assert ok[0].n_bad == 0 and bad[0].n_bad == 2
        assert bad[0].severity == "error" and bad[0].scope == "2025-26/regular"


def test_a4_is_a_warning():
    got, _ = _one(cg.check_a4_team_twice_same_day, "qc:A4", [("2019-20", "regular", 2118, 1)])
    assert got[0].severity == "warn" and got[0].n_bad == 1


def test_a7_reports_each_condition():
    cols = COLS + ["early_hour", "tip_null", "date_mismatch"]
    got, _ = _one(cg.check_a7_suspicious_tipoff, "qc:A7", [("2019-20", "regular", 1208, 4, 2, 1, 1)], cols)
    assert got[0].n_bad == 4 and got[0].severity == "warn"
    assert got[0].detail == {"early_hour": 2, "tip_null": 1, "date_mismatch": 1}


def test_c_checks():
    miss, _ = _one(cg.check_c1_status_rows_present, "qc:C1", [("2025-26", "playoffs", 85, 8)])
    assert miss[0].severity == "error" and miss[0].n_bad == 8
    c2, _ = _one(cg.check_c2_starters_are_ten, "qc:C2", [("2018-19", "regular", 1230, 19)])
    assert c2[0].severity == "warn" and c2[0].n_bad == 19
    c3, _ = _one(cg.check_c3_officials_at_least_three, "qc:C3", [("2020-21", "regular", 1080, 40)])
    assert c3[0].severity == "info" and c3[0].n_bad == 40


def test_every_check_survives_empty_data():
    empty = frame([], COLS)
    for fn, key in ((cg.check_a3_duplicate_games, "qc:A3"), (cg.check_a4_team_twice_same_day, "qc:A4"),
                    (cg.check_a5_stuck_not_final, "qc:A5"), (cg.check_a6_final_without_score, "qc:A6"),
                    (cg.check_c1_status_rows_present, "qc:C1"), (cg.check_c2_starters_are_ten, "qc:C2"),
                    (cg.check_c3_officials_at_least_three, "qc:C3")):
        run, _ = fake_run({key: empty})
        got = fn(run, ALL)
        assert len(got) == 1 and got[0].n_bad == 0 and got[0].n_total == 0
    run, _ = fake_run({"qc:A7": frame([], COLS + ["early_hour", "tip_null", "date_mismatch"])})
    assert cg.check_a7_suspicious_tipoff(run, ALL)[0].n_bad == 0


def test_scope_filters_are_passed_as_parameters():
    run, db = fake_run({"qc:A3": frame([], COLS)})
    cg.check_a3_duplicate_games(run, Scope(["2025-26"], date(2026, 10, 1)))
    sql, params = db.calls[0]
    assert "g.season = ANY(%s)" in sql and "g.game_date_et > %s" in sql
    assert params == (["2025-26"], date(2026, 10, 1))


def test_all_sql_is_select_only():
    assert_select_only(Path(cg.__file__))
