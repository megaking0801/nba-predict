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


# ---------- P2: price points, backfill only what is missing, guarded rebuild ----------

def _odds_item(spread_blocks: dict, total_blocks: dict | None = None, over_under=None):
    """Core-API odds item: spread under homeTeamOdds, totals under provider-level close/current/open."""
    def side(blocks):
        return {k: {"pointSpread": {"american": v}, "spread": {"decimal": "1.91"}}
                for k, v in blocks.items()}
    item = {"provider": {"priority": 0}, "homeTeamOdds": side(spread_blocks),
            "awayTeamOdds": side({k: "+3.5" for k in spread_blocks})}
    for k, tot in (total_blocks or {}).items():
        item[k] = {"total": {"american": tot}, "over": {"decimal": "1.9"}, "under": {"decimal": "1.9"}}
    if over_under is not None:
        item["overUnder"] = over_under
    return item


def test_parse_close_line_points():
    only_current = ingest_espn.parse_close_line(
        [_odds_item({"current": "-3.5"}, {"current": "220.5"})])
    assert only_current["price_point"] == "current"
    assert only_current["total_price_point"] == "current"
    mixed = ingest_espn.parse_close_line(
        [_odds_item({"close": "-4.5", "current": "-3.5"}, {"current": "221.5"})])
    assert mixed["price_point"] == "close" and mixed["total_price_point"] == "current"
    assert mixed["home_spread"] == -4.5 and mixed["total_line"] == 221.5
    no_total = ingest_espn.parse_close_line([_odds_item({"open": "-2.5"})])
    assert no_total["price_point"] == "open" and no_total["total_price_point"] is None
    assert no_total["total_line"] is None
    from_over_under = ingest_espn.parse_close_line(
        [_odds_item({"close": "-2.5"}, None, over_under="219.5")])
    assert from_over_under["total_line"] == 219.5
    assert from_over_under["total_price_point"] == "current"


def test_parse_close_line_without_spread_is_none():
    assert ingest_espn.parse_close_line([{"provider": {"priority": 0}}]) is None


def test_backfill_odds_requires_force(monkeypatch):
    import pytest
    with pytest.raises(RuntimeError, match="backfill_odds_missing"):
        ingest_espn.backfill_odds()


class _SqlLog:
    def __init__(self, games):
        self.games = games
        self.sql: list[str] = []
        self.commits = 0

    def cursor(self):
        log = self

        class Cur(_Cur):
            def execute(self, sql, params=None):
                log.sql.append(" ".join(sql.split()))

            def fetchall(self):
                return log.games

        return Cur([])

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def test_force_rebuild_deletes_only_backfilled_rows(monkeypatch):
    log = _SqlLog([])
    monkeypatch.setattr(ingest_espn, "db_connect", lambda: log)
    monkeypatch.setattr(ingest_espn.psycopg2.extras, "execute_values", lambda *a, **k: None)
    ingest_espn.backfill_odds(force_rebuild=True)
    deletes = [s for s in log.sql if s.startswith("DELETE")]
    assert len(deletes) == 1
    assert "ml.captured_at = g.tipoff_utc" in deletes[0] and "ml.source = 'espn'" in deletes[0]
    assert "DELETE FROM public.market_lines WHERE source='espn'" not in deletes[0]


def test_backfill_odds_missing_never_deletes(monkeypatch):
    now = dt.datetime(2022, 1, 1, 0, 0, tzinfo=UTC)
    log = _SqlLog([("g1", now, now.date()), ("g2", None, now.date())])
    monkeypatch.setattr(ingest_espn, "db_connect", lambda: log)
    monkeypatch.setattr(ingest_espn, "odds", lambda gid, **kw: {"items": []})
    monkeypatch.setattr(ingest_espn, "parse_close_line", lambda items: {
        "home_spread": -3.5, "home_price": 1.9, "away_price": 1.9, "total_line": 220.5,
        "over_price": 1.9, "under_price": 1.9, "price_point": "current",
        "total_price_point": "current"})
    rows: list = []
    monkeypatch.setattr(ingest_espn.psycopg2.extras, "execute_values",
                        lambda cur, sql, r, page_size=0: rows.extend(r))
    ingest_espn.backfill_odds_missing(["2021-22"])
    assert not any("DELETE" in s.upper() for s in log.sql)
    select = next(s for s in log.sql if s.startswith("SELECT"))
    assert "NOT EXISTS" in select and "ml.source = 'espn'" in select and "g.season = ANY" in select
    assert len(rows) == 2
    assert rows[0][1] == now                       # captured_at = tipoff
    assert rows[1][1].tzinfo is not None           # no tipoff: 23:00 ET fallback
    assert rows[0][-2:] == ("current", "current")  # price points are written


def test_main_seasons_runs_ingest_then_odds(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ingest_espn, "db_connect", lambda: _RecConn())
    monkeypatch.setattr(ingest_espn, "ensure_schema", lambda conn: None)
    monkeypatch.setattr(ingest_espn, "backfill", lambda seasons: calls.append(("backfill", list(seasons))))
    monkeypatch.setattr(ingest_espn, "backfill_odds_missing",
                        lambda seasons: calls.append(("odds", list(seasons))))
    ingest_espn.main(["--seasons", "2022-23,2021-22"])
    assert calls == [("backfill", ["2022-23"]), ("odds", ["2022-23"]),
                     ("backfill", ["2021-22"]), ("odds", ["2021-22"])]


def test_main_odds_flag_needs_force_rebuild(monkeypatch):
    import pytest
    monkeypatch.setattr(ingest_espn, "db_connect", lambda: _RecConn())
    monkeypatch.setattr(ingest_espn, "ensure_schema", lambda conn: None)
    with pytest.raises(RuntimeError):
        ingest_espn.main(["--odds"])
    seen = {}
    monkeypatch.setattr(ingest_espn, "backfill_odds", lambda force_rebuild=False: seen.update(f=force_rebuild))
    ingest_espn.main(["--odds", "--force-rebuild"])
    assert seen == {"f": True}


def test_snapshot_rows_carry_price_points(monkeypatch):
    now = dt.datetime.now(UTC)
    monkeypatch.setattr(ingest_espn, "db_connect", lambda: _Conn([("soon", now + dt.timedelta(minutes=60), now.date())]))
    monkeypatch.setattr(ingest_espn, "odds", lambda gid, **kw: {"items": []})
    monkeypatch.setattr(ingest_espn, "parse_close_line", lambda items: {
        "home_spread": -3.5, "home_price": 1.9, "away_price": 1.9, "total_line": None,
        "over_price": None, "under_price": None, "price_point": "current", "total_price_point": None})
    inserted: list = []
    monkeypatch.setattr(ingest_espn.psycopg2.extras, "execute_values",
                        lambda cur, sql, rows, page_size=0: inserted.extend(rows))
    ingest_espn.snapshot_espn_odds()
    assert inserted[0][-2:] == ("current", None)
    assert "price_point, total_price_point" in ingest_espn.INSERT_LINE


# ---------- P2 review follow-ups ----------

def test_snapshot_espn_odds_ensures_schema_first(monkeypatch):
    """--close runs can hit the DB before any other step created price_point."""
    order: list[str] = []
    monkeypatch.setattr(ingest_espn, "ensure_schema", lambda conn: order.append("schema"))
    now = dt.datetime.now(UTC)
    monkeypatch.setattr(ingest_espn, "db_connect", lambda: _Conn([("g", now + dt.timedelta(minutes=30), now.date())]))
    monkeypatch.setattr(ingest_espn, "odds", lambda gid, **kw: order.append("odds") or None)
    ingest_espn.snapshot_espn_odds(window_minutes=150)
    assert order[0] == "schema" and "odds" in order


def test_ingest_dates_replaces_derived_rows_before_upsert(monkeypatch):
    """Re-parsing a game must not leave stale status/official/shot rows behind."""
    import json
    from pathlib import Path
    sample = json.loads((Path(__file__).parent / "fixtures" / "espn" / "summary_final_sample.json")
                        .read_text(encoding="utf-8"))
    log: list[tuple] = []

    class Cur(_Cur):
        def execute(self, sql, params=None):
            log.append(("exec", " ".join(sql.split()), params))

    class Conn(_RecConn):
        def cursor(self):
            return Cur([])

    monkeypatch.setattr(ingest_espn, "scoreboard", lambda d, **kw: {"events": [_event(False)]})
    monkeypatch.setattr(ingest_espn, "summary", lambda gid, **kw: sample)
    monkeypatch.setattr(ingest_espn, "db_connect", lambda: Conn())
    monkeypatch.setattr(ingest_espn.psycopg2.extras, "execute_values",
                        lambda cur, sql, rows, page_size=0: log.append(("values", " ".join(sql.split())[:60], None)))
    ingest_espn.ingest_dates(["20260421"], finals_only=True)
    for table, upsert in (("player_game_status", "INSERT INTO public.player_game_status"),
                          ("game_officials", "INSERT INTO public.game_officials"),
                          ("team_shot_buckets", "INSERT INTO public.team_shot_buckets")):
        d = next(i for i, e in enumerate(log) if e[0] == "exec" and e[1].startswith(f"DELETE FROM public.{table}"))
        u = next(i for i, e in enumerate(log) if e[0] == "values" and e[1].startswith(upsert))
        assert d < u
        assert log[d][2] == (["401869396"],)


def test_backfill_unknown_season_raises():
    import pytest
    with pytest.raises(ValueError, match="2019-2020"):
        ingest_espn.backfill(["2019-2020"])


def test_force_rebuild_skips_games_with_live_lines_or_no_tipoff(monkeypatch):
    log = _SqlLog([])
    monkeypatch.setattr(ingest_espn, "db_connect", lambda: log)
    monkeypatch.setattr(ingest_espn.psycopg2.extras, "execute_values", lambda *a, **k: None)
    ingest_espn.backfill_odds(force_rebuild=True)
    select = next(s for s in log.sql if s.startswith("SELECT"))
    assert "ml.captured_at <> g.tipoff_utc" in select and "g.tipoff_utc IS NOT NULL" in select
