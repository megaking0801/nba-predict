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


# ---------- P2: windows, neutral site, new tables ----------

def test_season_windows_backfill():
    w = ingest_espn.SEASON_WINDOWS
    for y in (2015, 2016, 2017, 2018, 2021, 2022):
        season = f"{y}-{str(y + 1)[-2:]}"
        assert w[season] == (dt.date(y, 10, 1), dt.date(y + 1, 6, 30)), season
    assert w["2019-20"] == (dt.date(2019, 10, 1), dt.date(2020, 10, 15))
    assert w["2020-21"] == (dt.date(2020, 12, 1), dt.date(2021, 7, 31))
    assert w["2026-27"] == (dt.date(2026, 10, 1), dt.date(2027, 6, 30))


def _event(neutral=...):
    comp = {"date": "2026-04-21T23:00Z", "status": {"type": {"name": "STATUS_FINAL"}},
            "competitors": [
                {"homeAway": "home", "score": "100", "team": {"abbreviation": "BOS"}},
                {"homeAway": "away", "score": "90", "team": {"abbreviation": "PHI"}}]}
    if neutral is not ...:
        comp["neutralSite"] = neutral
    return {"id": "401869396", "season": {"year": 2026, "type": 2}, "competitions": [comp]}


def test_scoreboard_neutral_site():
    assert ingest_espn.parse_scoreboard_events({"events": [_event(True)]})[0]["neutral_site"] is True
    assert ingest_espn.parse_scoreboard_events({"events": [_event(False)]})[0]["neutral_site"] is False
    assert ingest_espn.parse_scoreboard_events({"events": [_event()]})[0]["neutral_site"] is None


def test_game_upsert_keeps_known_neutral_flag_when_missing():
    assert "neutral_site" in ingest_espn.UPSERT_GAMES
    assert "COALESCE(EXCLUDED.neutral_site, games_v2.neutral_site)" in ingest_espn.UPSERT_GAMES
    row = ingest_espn._game_row(ingest_espn.parse_scoreboard_events({"events": [_event(True)]})[0])
    assert row[-1] is True


class _RecConn:
    def __init__(self):
        self.commits = 0
        self.closed = False

    def cursor(self):
        return _Cur([])

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def test_ingest_dates_writes_new_tables(monkeypatch):
    import json
    from pathlib import Path
    sample = json.loads((Path(__file__).parent / "fixtures" / "espn" / "summary_final_sample.json")
                        .read_text(encoding="utf-8"))
    conn = _RecConn()
    monkeypatch.setattr(ingest_espn, "scoreboard", lambda d, **kw: {"events": [_event(False)]})
    monkeypatch.setattr(ingest_espn, "summary", lambda gid, **kw: sample)
    monkeypatch.setattr(ingest_espn, "db_connect", lambda: conn)
    written: dict = {}
    monkeypatch.setattr(ingest_espn.psycopg2.extras, "execute_values",
                        lambda cur, sql, rows, page_size=0: written.setdefault(
                            " ".join(sql.split())[:60], []).extend(rows))
    games, teams, players = ingest_espn.ingest_dates(["20260421"], finals_only=True)
    assert (games, teams) == (1, 2) and players > 10
    by_table = {k.split("(")[0].strip(): v for k, v in written.items()}
    assert len(by_table["INSERT INTO public.player_game_status"]) == 29
    assert len(by_table["INSERT INTO public.game_officials"]) == 3
    assert sum(r[5] for r in by_table["INSERT INTO public.team_shot_buckets"]) == 179
    assert conn.commits == 1 and conn.closed
    # games are written before the tables that reference them
    assert list(written)[0].startswith("INSERT INTO public.games_v2")


def test_new_upserts_are_idempotent():
    for sql in (ingest_espn.UPSERT_STATUS, ingest_espn.UPSERT_OFFICIALS, ingest_espn.UPSERT_SHOTS):
        assert "ON CONFLICT" in sql and "DO UPDATE" in sql


def test_daily_and_backfill_ensure_schema_first(monkeypatch):
    """New columns/tables must exist before the first write: run_daily does not
    call ensure_schema before the ESPN ingest step."""
    order: list[str] = []
    monkeypatch.setattr(ingest_espn, "ensure_schema", lambda conn: order.append("schema"))
    monkeypatch.setattr(ingest_espn, "db_connect", lambda: _RecConn())
    monkeypatch.setattr(ingest_espn, "ingest_dates",
                        lambda dates, finals_only: order.append("ingest") or (0, 0, 0))
    ingest_espn.daily(1, 1)
    assert order == ["schema", "ingest"]
    order.clear()
    ingest_espn.backfill(["2022-23"])
    assert order[0] == "schema" and "ingest" in order
