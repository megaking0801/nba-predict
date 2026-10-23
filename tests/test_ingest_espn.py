"""Tests for jobs.ingest_espn: season windows, outage handling, close-snapshot window."""
from __future__ import annotations

import datetime as dt

import pytest

from jobs import ingest_espn

UTC = dt.timezone.utc


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("db_connect must not be reached in this scenario")
    monkeypatch.setattr(ingest_espn, "db_connect", boom)


def _dates(n: int) -> list[str]:
    return [f"2026102{i}" for i in range(n)]


def _scoreboards(monkeypatch, results: list):
    """scoreboard(d) returns results[i] for the i-th date (None = fetch failed)."""
    table = {}

    def fake(date, **kw):
        return table[date]

    dates = _dates(len(results))
    table.update(dict(zip(dates, results)))
    monkeypatch.setattr(ingest_espn, "scoreboard", fake)
    return dates


def test_season_window_2026_27():
    assert ingest_espn.SEASON_WINDOWS["2026-27"] == (dt.date(2026, 10, 1), dt.date(2027, 6, 30))


def test_ingest_dates_raises_when_majority_failed(monkeypatch):
    dates = _scoreboards(monkeypatch, [None, None, None, {"events": []}, {"events": []}])
    with pytest.raises(RuntimeError) as err:
        ingest_espn.ingest_dates(dates, finals_only=False)
    for failed in dates[:3]:
        assert failed in str(err.value)


def test_ingest_dates_tolerates_minority_failure(monkeypatch):
    dates = _scoreboards(monkeypatch, [None, None, {"events": []}, {"events": []}, {"events": []}])
    assert ingest_espn.ingest_dates(dates, finals_only=False) == (0, 0, 0)


def test_ingest_dates_all_empty_is_ok(monkeypatch):
    dates = _scoreboards(monkeypatch, [{"events": []}] * 5)
    assert ingest_espn.ingest_dates(dates, finals_only=False) == (0, 0, 0)


def test_in_close_window():
    now = dt.datetime(2026, 10, 23, 22, 30, tzinfo=UTC)
    f = ingest_espn.in_close_window
    assert f(dt.datetime(2026, 10, 23, 23, 0, tzinfo=UTC), now, 150)
    assert f(dt.datetime(2026, 10, 24, 1, 0, tzinfo=UTC), now, 150)       # exactly 150 min
    assert not f(dt.datetime(2026, 10, 24, 1, 1, tzinfo=UTC), now, 150)
    assert not f(dt.datetime(2026, 10, 23, 22, 29, tzinfo=UTC), now, 150)  # already started
    assert not f(None, now, 150)


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)

    def commit(self):
        pass

    def close(self):
        pass


def test_snapshot_window_filters_games(monkeypatch):
    now = dt.datetime.now(UTC)
    today = now.date()
    games = [
        ("started", now - dt.timedelta(minutes=30), today),
        ("soon", now + dt.timedelta(minutes=60), today),
        ("later", now + dt.timedelta(minutes=300), today),
        ("no_tip", None, today),
    ]
    monkeypatch.setattr(ingest_espn, "db_connect", lambda: _Conn(games))
    asked: list[str] = []

    def fake_odds(gid, **kw):
        asked.append(gid)
        return {"items": []}

    monkeypatch.setattr(ingest_espn, "odds", fake_odds)
    monkeypatch.setattr(ingest_espn, "parse_close_line", lambda items: {
        "home_spread": -3.5, "home_price": -110, "away_price": -110,
        "total_line": 220.5, "over_price": -110, "under_price": -110})
    inserted: list = []
    monkeypatch.setattr(ingest_espn.psycopg2.extras, "execute_values",
                        lambda cur, sql, rows, page_size=0: inserted.extend(rows))
    ingest_espn.snapshot_espn_odds(window_minutes=150)
    assert asked == ["soon"]
    assert [r[0] for r in inserted] == ["soon"]


def test_snapshot_without_window_keeps_old_behavior(monkeypatch):
    now = dt.datetime.now(UTC)
    games = [("a", now - dt.timedelta(minutes=30), now.date()),
             ("b", now + dt.timedelta(minutes=300), now.date())]
    monkeypatch.setattr(ingest_espn, "db_connect", lambda: _Conn(games))
    asked: list[str] = []
    monkeypatch.setattr(ingest_espn, "odds", lambda gid, **kw: asked.append(gid) or None)
    ingest_espn.snapshot_espn_odds()
    assert sorted(asked) == ["a", "b"]
