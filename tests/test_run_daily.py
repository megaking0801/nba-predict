"""Tests for jobs.run_daily: ESPN-based steps and the close-snapshot mode."""
from __future__ import annotations

import pytest

from jobs import run_daily


def test_steps_use_espn():
    assert [n for n, _ in run_daily.STEPS] == [
        "ingest_espn", "settle", "snapshot_injuries", "snapshot_rosters",
        "espn_odds", "snapshot_lines", "predict_daily"]


def test_close_steps_never_predict():
    names = [n for n, _ in run_daily.CLOSE_STEPS]
    assert names == ["espn_odds_close", "snapshot_injuries", "snapshot_lines"]
    assert "predict_daily" not in names


def test_close_window_is_150():
    assert run_daily.CLOSE_WINDOW_MINUTES == 150


def test_main_close_runs_only_close_steps(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(run_daily, "STEPS", [("full", lambda: calls.append("full"))])
    monkeypatch.setattr(run_daily, "CLOSE_STEPS", [("close", lambda: calls.append("close"))])
    run_daily.main(["--close"])
    assert calls == ["close"]
    calls.clear()
    run_daily.main([])
    assert calls == ["full"]


def test_main_exits_nonzero_but_runs_every_step(monkeypatch):
    calls: list[str] = []

    def boom():
        calls.append("a")
        raise RuntimeError("x")

    monkeypatch.setattr(run_daily, "STEPS", [("a", boom), ("b", lambda: calls.append("b"))])
    with pytest.raises(SystemExit) as exc:
        run_daily.main([])
    assert exc.value.code == 1 and calls == ["a", "b"]


def test_ingest_step_calls_espn_daily(monkeypatch):
    seen = {}
    from jobs import ingest_espn
    monkeypatch.setattr(ingest_espn, "daily",
                        lambda days_back, days_ahead=2: seen.update(back=days_back, ahead=days_ahead))
    dict(run_daily.STEPS)["ingest_espn"]()
    assert seen == {"back": 3, "ahead": 2}


def test_close_odds_step_uses_window(monkeypatch):
    seen = {}
    from jobs import ingest_espn
    monkeypatch.setattr(ingest_espn, "snapshot_espn_odds",
                        lambda days_back=3, days_ahead=2, window_minutes=None: seen.update(w=window_minutes))
    dict(run_daily.CLOSE_STEPS)["espn_odds_close"]()
    assert seen == {"w": 150}
