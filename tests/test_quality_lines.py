"""Tests for jobs.quality.checks_lines (E checks)."""
from __future__ import annotations

from pathlib import Path

from jobs.quality import checks_lines as cl
from jobs.quality.core import Scope
from tests.quality_fakes import assert_select_only, fake_run, frame

ALL = Scope(None, None)
G = ["season", "season_type", "n_total", "n_bad"]
HEALTH = ["season", "n", "corr_v", "mae", "bias"]


def test_e1_e2_grouped_checks():
    run, _ = fake_run({"qc:E1": frame([("2021-22", "regular", 1230, 7)], G)})
    f = cl.check_e1_line_coverage(run, ALL)[0]
    assert f.severity == "warn" and f.n_bad == 7
    run, db = fake_run({"qc:E2": frame([("2021-22", "regular", 1223, 0)], G)})
    f = cl.check_e2_spread_range(run, ALL)[0]
    assert f.severity == "error" and "v_closing_lines" in db.calls[0][0] and "> 25" in db.calls[0][0]


def test_e3_in_range_is_ok_and_each_violation_counts():
    ok = frame([("2022-23", 1230, 0.409, 9.78, 0.29)], HEALTH)
    run, db = fake_run({"qc:E3": ok})
    f = cl.check_e3_spread_health(run, ALL)[0]
    assert f.n_bad == 0 and f.severity == "error" and f.scope == "2022-23/regular"
    assert "season_type = 'regular'" in db.calls[0][0]
    # (corr, mae, bias) -> number of violated metrics
    for row, bad in (((0.90, 1.2, 0.0), 2), ((0.45, 10.0, 1.2), 1), ((0.45, 10.0, -1.2), 1), ((0.2, 13.0, 0.0), 2)):
        run, _ = fake_run({"qc:E3": frame([("2022-23", 1000) + row], HEALTH)})
        assert cl.check_e3_spread_health(run, ALL)[0].n_bad == bad, row


def test_e3_small_samples_are_info_not_errors():
    run, _ = fake_run({"qc:E3": frame([("2026-27", 11, 0.702, 6.23, -2.95)], HEALTH)})
    f = cl.check_e3_spread_health(run, ALL)[0]
    assert f.severity == "info" and f.n_bad == 0 and f.detail["n"] == 11


def test_e4_covers_every_season_type_and_reports_null_totals():
    cols = G + ["null_total"]
    run, db = fake_run({"qc:E4": frame([("2021-22", "preseason", 40, 2, 3), ("2021-22", "regular", 1223, 24, 0)], cols)})
    got = cl.check_e4_total_range(run, ALL)
    assert {f.scope for f in got} == {"2021-22/preseason", "2021-22/regular"}
    assert got[1].n_bad == 24 and got[1].severity == "error" and got[0].detail["null_total"] == 3
    sql = db.calls[0][0]
    assert "170" in sql and "280" in sql and "season_type = 'regular'" not in sql


def test_e5_excludes_e4_rows_and_judges_correlation():
    cols = ["season", "n", "corr_total", "mean_err"]
    run, db = fake_run({"qc:E5": frame([("2021-22", 1199, 0.146, 3.62)], cols)})
    f = cl.check_e5_total_health(run, ALL)[0]
    assert f.severity == "warn" and f.n_bad == 2 and f.scope == "2021-22/regular"
    assert "BETWEEN 170 AND 280" in db.calls[0][0]
    run, _ = fake_run({"qc:E5": frame([("2022-23", 1230, 0.355, 1.0)], cols)})
    assert cl.check_e5_total_health(run, ALL)[0].n_bad == 0
    run, _ = fake_run({"qc:E5": frame([("2026-27", 11, 0.1, 9.0)], cols)})
    assert cl.check_e5_total_health(run, ALL)[0].severity == "info"


def test_e6_flags_unlabeled_rows_not_at_tipoff():
    cols = G + ["n_null", "n_close", "n_current", "n_open"]
    run, _ = fake_run({"qc:E6": frame([("2023-24", "regular", 1383, 0, 1383, 0, 0, 0)], cols)})
    f = cl.check_e6_price_point_provenance(run, ALL)[0]
    assert f.severity == "warn" and f.n_bad == 0 and f.detail["n_null"] == 1383
    run, _ = fake_run({"qc:E6": frame([("2026-27", "regular", 11, 2, 0, 9, 2, 0)], cols)})
    assert cl.check_e6_price_point_provenance(run, ALL)[0].n_bad == 2


def test_e7_negative_correlation_is_an_error():
    cols = ["season", "n", "corr_v"]
    run, _ = fake_run({"qc:E7": frame([("2022-23", 1230, -0.2)], cols)})
    f = cl.check_e7_line_flip(run, ALL)[0]
    assert f.severity == "error" and f.n_bad == 1
    run, _ = fake_run({"qc:E7": frame([("2022-23", 1230, 0.41)], cols)})
    assert cl.check_e7_line_flip(run, ALL)[0].n_bad == 0


def test_empty_data_gives_zero_findings():
    for fn, key, cols in ((cl.check_e1_line_coverage, "qc:E1", G), (cl.check_e2_spread_range, "qc:E2", G),
                          (cl.check_e3_spread_health, "qc:E3", HEALTH),
                          (cl.check_e4_total_range, "qc:E4", G + ["null_total"]),
                          (cl.check_e5_total_health, "qc:E5", ["season", "n", "corr_total", "mean_err"]),
                          (cl.check_e6_price_point_provenance, "qc:E6", G + ["n_null", "n_close", "n_current", "n_open"]),
                          (cl.check_e7_line_flip, "qc:E7", ["season", "n", "corr_v"])):
        run, _ = fake_run({key: frame([], cols)})
        got = fn(run, ALL)
        assert len(got) == 1 and got[0].n_bad == 0 and got[0].n_total == 0


def test_all_sql_is_select_only():
    assert_select_only(Path(cl.__file__))
