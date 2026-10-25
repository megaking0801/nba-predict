"""Tests for jobs.quality.runner (CLI, baseline writing, D2 rule) and checks_espn (A1)."""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

from jobs.quality import checks_espn, runner
from jobs.quality.core import Finding, Scope
from tests.quality_fakes import fake_run, frame

ALL = Scope(None, None)


def _f(check, scope, n_bad, n_total=100, severity="warn", **detail):
    return Finding(check, severity, scope, n_bad, n_total, dict(detail))


# ---------- baseline application ----------

def test_run_applies_baseline_states(monkeypatch):
    findings = [_f("B1", "2015-16/regular", 322, 2460), _f("B1", "2016-17/regular", 400, 2460),
                _f("B5", "2025-26/regular", 1, 1000, severity="error"), _f("B2", "2025-26/regular", 0)]
    monkeypatch.setattr(runner, "REGISTRY", [("X", lambda run_sql, scope: findings)])
    baseline = {"as_of_date": "2026-10-24", "rates": {},
                "entries": {"B1|2015-16/regular": {"n_bad": 322}, "B1|2016-17/regular": {"n_bad": 336}}}
    got = runner.run(None, ALL, baseline)
    assert [f.status for f in got] == ["known", "worse", "new", "ok"]


def test_since_mode_uses_the_season_type_rate(monkeypatch):
    chronic = _f("B4", "2026-27/regular", 2, 140)
    zero_base = _f("B6", "2026-27/regular", 1, 140, severity="error")
    monkeypatch.setattr(runner, "REGISTRY", [("X", lambda run_sql, scope: [chronic, zero_base])])
    baseline = {"as_of_date": "2026-10-24", "entries": {}, "rates": {"B4|regular": 0.008}}
    got = runner.run(None, Scope(None, date(2026, 10, 24)), baseline)
    assert [f.status for f in got] == ["known", "new"]


# ---------- D2 rule ----------

def test_d2_regression_threshold():
    entry = {"n_bad": 541, "exact_pct": 52.2}
    same = _f("D2", "2015-16/regular", 541, 2138, exact_pct=52.2, n_comparable=2138)
    drop3 = _f("D2", "2015-16/regular", 541, 2138, exact_pct=49.2, n_comparable=2138)
    drop31 = _f("D2", "2015-16/regular", 541, 2138, exact_pct=49.1, n_comparable=2138)
    assert runner.classify_d2(same, entry).status == "known"
    assert runner.classify_d2(drop3, entry).status == "known"        # exactly 3.0 points is still known
    assert runner.classify_d2(drop31, entry).status == "worse"
    assert runner.classify_d2(_f("D2", "x", 0, 0, n_comparable=0), None).status == "ok"
    assert runner.classify_d2(_f("D2", "x", 20, 300, exact_pct=90.0, major_pct=6.7, n_comparable=300), None).status == "new"
    assert runner.classify_d2(_f("D2", "x", 5, 50, severity="info", n_comparable=50), None).status == "info"


# ---------- since date, baseline writing ----------

def test_since_date_is_max_of_baseline_and_window():
    today = date(2026, 11, 20)
    assert runner.since_date("7d", "2026-11-18", today) == date(2026, 11, 18)     # baseline is newer
    assert runner.since_date("7d", "2026-10-01", today) == date(2026, 11, 13)     # window is newer
    assert runner.since_date("7d", None, today) == date(2026, 11, 13)
    assert runner.since_date("30d", "2026-11-18", today) == date(2026, 11, 18)


def test_write_baseline_uses_latest_final_date_and_recent_rate(tmp_path):
    findings = [
        _f("B4", "2024-25/regular", 0, 2780), _f("B4", "2025-26/regular", 23, 2774),
        _f("B4", "2026-27/regular", 1, 200),                                     # partial season: not "recent complete"
        _f("B3", "2018-19/regular", 23, 2700),
        _f("D2", "2015-16/regular", 541, 2138, exact_pct=52.2, n_comparable=2138),
        _f("A5", "2025-26/regular", 0, 1000, severity="error"),
        _f("D3", "2025-26/regular", 936, 2462, severity="info"),
    ]
    data = runner.build_baseline(findings, date(2026, 10, 24))
    assert data["as_of_date"] == "2026-10-24"
    assert data["entries"]["B4|2025-26/regular"]["n_bad"] == 23
    assert data["entries"]["D2|2015-16/regular"]["exact_pct"] == 52.2
    assert "A5|2025-26/regular" not in data["entries"]                            # zero: not a known problem
    assert not any(k.startswith("D3|") for k in data["entries"])                  # info is never baselined
    assert abs(data["rates"]["B4|regular"] - 23 / 2774) < 1e-9                    # 2025-26, the latest full season
    path = tmp_path / "b.json"
    runner.write_baseline(path, data)
    assert json.loads(path.read_text(encoding="utf-8"))["entries"]["B3|2018-19/regular"]["n_bad"] == 23


# ---------- main ----------

class _Conn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _patch_db(monkeypatch, run_fn, conn=None):
    conn = conn or _Conn()
    monkeypatch.setattr(runner, "connect_readonly", lambda: conn)
    monkeypatch.setattr(runner, "make_run", lambda c: run_fn)
    return conn


def test_main_exit_codes(monkeypatch, tmp_path, capsys):
    run_fn, _ = fake_run({"qc:ASOF": frame([(date(2026, 10, 24),)], ["d"])})
    conn = _patch_db(monkeypatch, run_fn)
    new_warn = _f("B1", "2025-26/regular", 3, 100)
    monkeypatch.setattr(runner, "REGISTRY", [("B1", lambda r, s: [new_warn])])
    base = tmp_path / "none.json"
    assert runner.main(["--all", "--baseline", str(base), "--fail-on", "warn"]) == 1
    assert runner.main(["--all", "--baseline", str(base), "--fail-on", "error"]) == 0
    assert runner.main(["--all", "--baseline", str(base), "--fail-on", "none"]) == 0
    assert conn.closed


def test_main_writes_json(monkeypatch, tmp_path):
    run_fn, _ = fake_run({"qc:ASOF": frame([(date(2026, 10, 24),)], ["d"])})
    _patch_db(monkeypatch, run_fn)
    monkeypatch.setattr(runner, "REGISTRY", [("B1", lambda r, s: [_f("B1", "2025-26/regular", 0, 100)])])
    out = tmp_path / "out.json"
    assert runner.main(["--all", "--baseline", str(tmp_path / "n.json"), "--json", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))[0]["check_id"] == "B1"


def test_since_mode_reports_no_new_data(monkeypatch, tmp_path, capsys):
    run_fn, db = fake_run({"qc:NEW": frame([(0,)], ["n"])})
    _patch_db(monkeypatch, run_fn)
    monkeypatch.setattr(runner, "_today", lambda: date(2026, 11, 20))
    monkeypatch.setattr(runner, "REGISTRY", [("B1", lambda r, s: (_ for _ in ()).throw(AssertionError("must not run")))])
    base = tmp_path / "b.json"
    base.write_text(json.dumps({"as_of_date": "2026-11-18", "entries": {}}), encoding="utf-8")
    assert runner.main(["--since", "7d", "--baseline", str(base)]) == 0
    assert "無新資料" in capsys.readouterr().out
    assert db.calls[0][1] == (date(2026, 11, 18),)


def test_main_does_not_leak_dsn(monkeypatch, capsys):
    def boom():
        raise RuntimeError("postgresql://u:secret@h/db")
    monkeypatch.setattr(runner, "connect_readonly", boom)
    assert runner.main(["--all"]) == 2
    out = capsys.readouterr()
    assert "secret" not in out.out + out.err and "RuntimeError" in out.out + out.err


def test_write_baseline_writes_file_only(monkeypatch, tmp_path):
    run_fn, db = fake_run({"qc:ASOF": frame([(date(2026, 10, 24),)], ["d"])})
    _patch_db(monkeypatch, run_fn)
    monkeypatch.setattr(runner, "REGISTRY", [("B1", lambda r, s: [_f("B1", "2015-16/regular", 322, 2460)])])
    target = tmp_path / "baseline.json"
    assert runner.main(["--all", "--baseline", str(tmp_path / "x.json"), "--write-baseline", str(target)]) == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["as_of_date"] == "2026-10-24" and data["entries"]["B1|2015-16/regular"]["n_bad"] == 322
    assert all("INSERT" not in sql.upper() and "UPDATE " not in sql.upper() for sql, _ in db.calls)


def test_verify_flags_flag_exit_code(monkeypatch):
    from jobs.quality import game_quality as gq
    ok = pd.DataFrame([dict(gq.derive_flags(_raw()), **_raw())])
    _patch_db(monkeypatch, lambda sql, params=(): ok)
    assert runner.main(["--all", "--verify-flags"]) == 0
    bad = ok.copy()
    bad.loc[0, "usable_team_features"] = not bad.loc[0, "usable_team_features"]
    _patch_db(monkeypatch, lambda sql, params=(): bad)
    assert runner.main(["--all", "--verify-flags"]) == 1


def _raw():
    import datetime as dt
    return dict(game_id="g1", season="2025-26", season_type="regular", game_date_et=date(2025, 12, 1),
                raw_status="final", raw_team_rows=2, raw_team_nonnull_n=2, raw_player_min_nonnull_min=9,
                raw_player_pts_diff=0, raw_box_fga_nonnull=True, raw_bucket_diff_max=0, raw_bucket_rows=2,
                raw_status_rows=26, raw_starters_n=10, raw_officials_n=3, raw_spread=-3.5, raw_total=224.5,
                raw_line_price_point="close", raw_line_total_price_point="close",
                raw_line_captured_at=dt.datetime(2025, 12, 2, tzinfo=dt.timezone.utc),
                raw_tipoff_utc=dt.datetime(2025, 12, 2, 0, 30, tzinfo=dt.timezone.utc))


# ---------- A1: ESPN coverage ----------

def _event(eid, abbrs, *, stype=2, status="STATUS_FINAL", year=2026):
    return {"id": eid, "season": {"year": year, "type": stype},
            "competitions": [{"status": {"type": {"name": status}},
                              "competitors": [{"homeAway": h, "team": {"abbreviation": a}}
                                              for h, a in zip(("home", "away"), abbrs)]}]}


def _scoreboard(events_by_date):
    return lambda d, **kw: ({"events": events_by_date.get(d, [])} if d in events_by_date or True else None)


def _a1(events, db_rows, scope=None, fetch=None):
    run_fn, _ = fake_run({"qc:A1": frame(db_rows, ["season", "season_type", "n"])})
    scope = scope or Scope(["2025-26"], date(2026, 2, 14))
    fetch = fetch or (lambda d, **kw: {"events": events} if d == "20260215" else {"events": []})
    return checks_espn.check_a1_espn_event_coverage(
        run_fn, scope, fetch, today=date(2026, 2, 16))


def test_a1_clean_coverage_and_allowed_skips():
    events = [_event("1", ["LAL", "BOS"]),
              _event("2", ["EAST", "WEST"]),                       # all-star game: both teams non-NBA
              _event("3", ["MEL", "OKC"], stype=1),                # preseason international
              _event("4", ["TBD", "TBD"], status="STATUS_SCHEDULED")]
    got = {f.scope: f for f in _a1(events, [("2025-26", "regular", 1)])}
    assert got["2025-26/regular"].n_bad == 0 and got["2025-26/regular"].n_total == 1
    assert got["events/skipped"].n_bad == 0


def test_a1_detects_missing_game_and_unknown_skip_type():
    events = [_event("1", ["LAL", "BOS"]), _event("5", ["LAL", "NEW"])]
    got = {f.scope: f for f in _a1(events, [("2025-26", "regular", 0)])}
    assert got["2025-26/regular"].n_bad == 1                       # ESPN has a game the DB lacks
    assert got["events/skipped"].n_bad == 1 and got["events/skipped"].severity == "error"
    assert got["events/skipped"].detail["examples"][0]["abbrs"] == ["LAL", "NEW"]


def test_a1_reports_failed_fetches():
    got = {f.scope: f for f in _a1([], [], fetch=lambda d, **kw: None)}
    assert got["events/fetch"].n_bad == got["events/fetch"].n_total > 0


def test_a1_is_skipped_without_the_flag(monkeypatch):
    run_fn, _ = fake_run({"qc:ASOF": frame([(date(2026, 10, 24),)], ["d"])})
    _patch_db(monkeypatch, run_fn)
    called = []
    monkeypatch.setattr(runner, "REGISTRY", [])
    monkeypatch.setattr(runner, "check_a1_espn_event_coverage", lambda *a, **k: called.append(1) or [])
    runner.main(["--all"])
    assert not called
    runner.main(["--all", "--with-espn"])
    assert called


def test_write_baseline_refuses_a_window(monkeypatch, tmp_path, capsys):
    called = []
    monkeypatch.setattr(runner, "connect_readonly", lambda: called.append(1))
    assert runner.main(["--since", "7d", "--write-baseline", str(tmp_path / "b.json")]) == 2
    assert not called and "full run" in capsys.readouterr().out


# ---------- review fixes ----------

def test_since_mode_rate_tolerance_never_applies_to_error_checks(monkeypatch):
    chronic_error = _f("C1", "2027-28/playoffs", 3, 25, severity="error")
    chronic_warn = _f("B4", "2027-28/playoffs", 3, 25, severity="warn")
    monkeypatch.setattr(runner, "REGISTRY", [("X", lambda r, s: [chronic_error, chronic_warn])])
    baseline = {"as_of_date": "2026-10-24", "entries": {},
                "rates": {"C1|playoffs": 0.094, "B4|playoffs": 0.094}}
    got = runner.run(None, Scope(None, date(2026, 10, 24)), baseline)
    assert [f.status for f in got] == ["new", "known"]


def test_baseline_rates_only_for_warn_checks_and_complete_seasons():
    findings = [
        _f("C1", "2025-26/playoffs", 8, 85, severity="error"),
        _f("B4", "2025-26/regular", 23, 2774),
        _f("B4", "2026-27/regular", 5, 200),                       # season still running on 2026-10-24
        _f("B4", "2025-26/preseason", 3, 130),
        _f("B4", "2024-25/preseason", 2, 138),
    ]
    data = runner.build_baseline(findings, date(2026, 10, 24))
    assert "C1|playoffs" not in data["rates"]
    assert abs(data["rates"]["B4|regular"] - 23 / 2774) < 1e-9
    assert abs(data["rates"]["B4|preseason"] - 3 / 130) < 1e-9      # latest finished season, not the biggest


def test_d2_new_season_without_baseline_tolerates_a_small_bad_share():
    small = _f("D2", "2027-28/regular", 5, 300, exact_pct=60.0, major_pct=1.7, none_pct=0.0, n_comparable=300)
    big = _f("D2", "2027-28/regular", 20, 300, exact_pct=60.0, major_pct=6.7, none_pct=0.0, n_comparable=300)
    assert runner.classify_d2(small, None).status == "ok"
    assert runner.classify_d2(big, None).status == "new"


def test_no_new_data_shortcut_counts_games_not_only_final_ones(monkeypatch, tmp_path):
    run_fn, db = fake_run({"qc:NEW": frame([(5,)], ["n"])})
    _patch_db(monkeypatch, run_fn)
    monkeypatch.setattr(runner, "_today", lambda: date(2026, 11, 20))
    monkeypatch.setattr(runner, "REGISTRY", [("B1", lambda r, s: [_f("B1", "2026-27/regular", 0, 10)])])
    base = tmp_path / "b.json"
    base.write_text(json.dumps({"as_of_date": "2026-11-18", "entries": {}}), encoding="utf-8")
    assert runner.main(["--since", "7d", "--baseline", str(base)]) == 0
    sql = db.calls[0][0]
    assert "status" not in sql and "current_date" in sql      # stuck games must still reach A5


def test_errors_after_connecting_keep_their_message_but_not_a_dsn(monkeypatch, capsys):
    run_fn, _ = fake_run({"qc:ASOF": frame([(date(2026, 10, 24),)], ["d"])})
    _patch_db(monkeypatch, run_fn)

    def boom(r, s):
        raise ValueError('column "price_point" does not exist (postgresql://u:secret@h/db)')

    monkeypatch.setattr(runner, "REGISTRY", [("E6", boom)])
    assert runner.main(["--all"]) == 2
    out = capsys.readouterr().out
    assert 'column "price_point" does not exist' in out and "E6" in out and "ValueError" in out
    assert "secret" not in out


def test_write_baseline_needs_all_seasons(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runner, "connect_readonly", lambda: (_ for _ in ()).throw(AssertionError("no db")))
    assert runner.main(["--seasons", "2025-26", "--write-baseline", str(tmp_path / "b.json")]) == 2
    assert "--all" in capsys.readouterr().out


def test_a1_queries_the_db_before_the_network_and_dedups_events():
    order = []
    run_fn, db = fake_run({"qc:A1": lambda p: (order.append("db"), frame([("2025-26", "regular", 1)],
                                               ["season", "season_type", "n"]))[1]})

    def fetch(d, **kw):
        order.append("espn")
        return {"events": [_event("1", ["LAL", "BOS"])]}          # the same event under every date

    got = {f.scope: f for f in checks_espn.check_a1_espn_event_coverage(
        run_fn, Scope(["2025-26"], date(2026, 2, 13)), fetch, today=date(2026, 2, 16))}
    assert order[0] == "db"
    assert got["2025-26/regular"].n_total == 1 and got["2025-26/regular"].n_bad == 0   # counted once
    assert "ANY(%s)" in db.calls[0][0]
