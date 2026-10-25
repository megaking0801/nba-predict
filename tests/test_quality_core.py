"""Tests for jobs.quality.core: Finding, baseline state machine, exit codes."""
from __future__ import annotations

import json
from datetime import date

from jobs.quality import core
from jobs.quality.core import Finding, Scope, classify, exit_code


def _f(n_bad, n_total=100, severity="warn", **detail) -> Finding:
    return Finding("B3", severity, "2025-26/regular", n_bad, n_total, dict(detail))


def test_classify_ok_known_worse_new():
    assert classify(_f(0), None).status == "ok"
    assert classify(_f(5), None).status == "new"
    assert classify(_f(5), {"n_bad": 5}).status == "known"
    assert classify(_f(3), {"n_bad": 5}).status == "known"
    assert classify(_f(6), {"n_bad": 5}).status == "worse"
    got = classify(_f(6), {"n_bad": 5})
    assert got.baseline == 5 and got.n_bad == 6


def test_classify_does_not_mutate_input():
    f = _f(6)
    classify(f, {"n_bad": 5})
    assert f.status == "ok" and f.baseline is None


def test_info_never_fails():
    assert classify(_f(50, severity="info"), None).status == "info"
    assert classify(_f(50, severity="info"), {"n_bad": 1}, since_mode=True).status == "info"
    assert exit_code([classify(_f(50, severity="info"), None)], "warn") == 0


def test_improved_flag():
    got = classify(_f(0), {"n_bad": 5})
    assert got.status == "ok" and got.detail["improved"] is True
    assert "improved" not in classify(_f(0), None).detail


def test_since_mode_tolerates_chronic_rate():
    entry = {"recent_rate": 0.008}
    # allowed = ceil(140 * 0.008 * 3) + 2 = 4 + 2 = 6 ... n_bad 2 is fine, 9 is not
    assert classify(_f(2, 140), entry, since_mode=True).status == "known"
    assert classify(_f(9, 140), entry, since_mode=True).status == "worse"
    assert core.allowed_in_window(140, 0.008) == 6


def test_since_mode_zero_baseline_is_new():
    assert classify(_f(1, 140, severity="error"), None, since_mode=True).status == "new"
    assert classify(_f(0, 140, severity="error"), None, since_mode=True).status == "ok"


def test_since_mode_entry_without_rate():
    assert classify(_f(1), {"n_bad": 3}, since_mode=True).status == "worse"
    assert classify(_f(1), {"recent_rate": 0}, since_mode=True).status == "worse"


def test_exit_code_fail_on():
    new_warn = classify(_f(3, severity="warn"), None)
    known_error = classify(_f(3, severity="error"), {"n_bad": 3})
    new_error = classify(_f(3, severity="error"), None)
    assert new_warn.status == "new"
    assert exit_code([new_warn], "error") == 0
    assert exit_code([new_warn], "warn") == 1
    assert exit_code([known_error], "warn") == 0         # known problems never fail
    assert exit_code([new_error], "error") == 1
    assert exit_code([new_error], "none") == 0


def test_baseline_key_and_scope_where():
    assert core.baseline_key("B3", "2025-26/regular") == "B3|2025-26/regular"
    sql, params = core.scope_where(Scope(["2025-26"], date(2026, 10, 1)))
    assert "g.season = ANY(%s)" in sql and "g.game_date_et > %s" in sql
    assert params == (["2025-26"], date(2026, 10, 1))
    assert core.scope_where(Scope(None, None)) == ("", ())


def test_load_baseline_missing_file(tmp_path):
    assert core.load_baseline(None) == {"as_of_date": None, "entries": {}, "rates": {}}
    assert core.load_baseline(tmp_path / "nope.json") == {"as_of_date": None, "entries": {}, "rates": {}}


def test_load_baseline_roundtrip(tmp_path):
    path = tmp_path / "b.json"
    path.write_text(json.dumps({"as_of_date": "2026-10-24",
                                "entries": {"B1|2015-16/regular": {"n_bad": 322, "why": "x"}}}),
                    encoding="utf-8")
    got = core.load_baseline(path)
    assert got["as_of_date"] == "2026-10-24" and got["rates"] == {}
    assert got["entries"]["B1|2015-16/regular"]["n_bad"] == 322


def test_findings_from_frame_handles_empty_and_zero_total():
    import pandas as pd
    empty = core.findings_from_frame(pd.DataFrame(), "A3", "error", "n_bad", "n_total")
    assert len(empty) == 1 and empty[0].n_bad == 0 and empty[0].n_total == 0
    df = pd.DataFrame([{"season": "2026-27", "season_type": "playin", "n_total": 0, "n_bad": 0}])
    got = core.findings_from_frame(df, "A3", "error", "n_bad", "n_total")
    assert got[0].scope == "2026-27/playin" and got[0].n_total == 0
