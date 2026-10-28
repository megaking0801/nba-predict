"""Tests for the injury / roster snapshot jobs and their schema (no network, no DB)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from jobs import schema, snapshot_injuries

UTC = dt.timezone.utc
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "espn"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------- schema ----------

def test_schema_has_new_tables_idempotent():
    assert "CREATE TABLE IF NOT EXISTS public.injury_reports" in schema.DDL
    assert "CREATE TABLE IF NOT EXISTS public.roster_snapshots" in schema.DDL
    assert schema.SCHEMA_VERSION == "4"
    assert "DROP" not in schema.MIGRATIONS.upper()


def test_old_injury_table_unchanged():
    assert "UNIQUE (snapshot_date_et, source, team_abbr, player_name)" in schema.DDL


# ---------- injuries ----------

TODAY = dt.date(2026, 10, 23)


def test_parse_injuries_reads_reported_at():
    rows = snapshot_injuries.parse_injuries(_load("injuries_sample.json"), TODAY)
    by_name = {r["player_name"]: r for r in rows}
    # sample values: Gueye's report is dated "2026-10-19T19:38Z"; Coby White's `date` was removed
    assert by_name["Mouhamed Gueye"]["reported_at"] == dt.datetime(2026, 10, 19, 19, 38, tzinfo=UTC)
    assert by_name["Coby White"]["reported_at"] is None
    assert {r["team_abbr"] for r in rows} == {"ATL", "CHA"}
    assert all(r["snapshot_date_et"] == TODAY for r in rows)
    assert by_name["Mouhamed Gueye"]["status"] == "Out"
    assert by_name["Mouhamed Gueye"]["detail"] == "Fracture"


def _live_shaped_payload():
    """Shape of the live ESPN injuries response (2026-10-28): no athlete.id, the entry's own "id" is
    the report's, and the player id is only in the athlete's links."""
    link = lambda rel, href: {"rel": rel, "href": href, "text": "x"}      # noqa: E731
    athlete = {"displayName": "Aaron Wiggins", "links": [
        link(["stats", "desktop", "athlete"], "https://www.espn.com/nba/player/stats/_/id/4397183/aaron-wiggins"),
        link(["playercard", "desktop", "athlete"], "https://www.espn.com/nba/player/_/id/4397183/aaron-wiggins"),
        link(["stats", "sportscenter", "app", "athlete"], "sportscenter://x-callback-url/showClubhouse?uid=s:40~l:46~a:4397183")]}
    return {"injuries": [{"id": "1", "displayName": "Atlanta Hawks", "injuries": [
        {"id": "536368", "status": "Day-To-Day", "date": "2026-10-27T21:07Z", "athlete": athlete,
         "details": {"detail": "Knee"}}]}]}


def test_parse_injuries_reads_player_id_from_links_not_the_report_id():
    rows = snapshot_injuries.parse_injuries(_live_shaped_payload(), TODAY)
    assert rows[0]["espn_player_id"] == 4397183 != 536368


def test_player_id_prefers_athlete_id_and_tolerates_missing_links():
    assert snapshot_injuries._player_id({"id": "77", "links": []}) == 77
    assert snapshot_injuries._player_id({"displayName": "x"}) is None
    assert snapshot_injuries._player_id({"links": [{"rel": ["stats"], "href": "https://e/_/id/12/x"}]}) == 12
    assert snapshot_injuries._player_id({"links": [{"href": "sportscenter://x?uid=s:40~a:99"}]}) is None


def test_parse_injuries_bad_date_does_not_break_batch():
    payload = _load("injuries_sample.json")
    payload["injuries"][0]["injuries"][0]["date"] = "not-a-date"
    rows = snapshot_injuries.parse_injuries(payload, TODAY)
    assert len(rows) == 4
    assert next(r for r in rows if r["player_name"] == "Mouhamed Gueye")["reported_at"] is None


def test_parse_injuries_skips_unknown_team(capsys):
    payload = _load("injuries_sample.json")
    payload["injuries"][0]["displayName"] = "Nowhere Nobodies"
    rows = snapshot_injuries.parse_injuries(payload, TODAY)
    assert {r["team_abbr"] for r in rows} == {"CHA"}
    assert "unknown injury team name" in capsys.readouterr().out


def test_injury_reports_insert_is_append():
    assert "INSERT INTO public.injury_reports" in snapshot_injuries.INSERT_REPORT_SQL
    assert "ON CONFLICT" not in snapshot_injuries.INSERT_REPORT_SQL


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))


class _Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cur(self.log)

    def commit(self):
        pass

    def close(self):
        pass


def test_main_writes_both_tables_with_one_timestamp(monkeypatch):
    log: list = []
    payload = _load("injuries_sample.json")
    monkeypatch.setattr(snapshot_injuries.requests, "get", lambda *a, **k: _Resp(payload))
    monkeypatch.setattr(snapshot_injuries, "db_connect", lambda: _Conn(log))
    monkeypatch.setattr(snapshot_injuries, "ensure_schema", lambda conn: None)
    snapshot_injuries.main()
    old = [p for sql, p in log if "INSERT INTO public.injury_snapshots" in sql]
    new = [p for sql, p in log if "INSERT INTO public.injury_reports" in sql]
    assert len(old) == 4 and len(new) == 4
    captured = {p[0] for p in new}
    assert len(captured) == 1 and next(iter(captured)).tzinfo is not None


# ---------- rosters ----------

from jobs import snapshot_rosters  # noqa: E402


def test_parse_teams_maps_30_teams():
    ids = snapshot_rosters.parse_teams(_load("teams_sample.json"))
    assert len(ids) == 30
    assert ids["LAL"] == "13" and ids["NYK"] == "18" and ids["GSW"] == "9"


def test_parse_teams_unknown_abbr_raises():
    import pytest
    payload = _load("teams_sample.json")
    payload["sports"][0]["leagues"][0]["teams"][0]["team"]["abbreviation"] = "ZZZ"
    with pytest.raises(ValueError, match="ZZZ"):
        snapshot_rosters.parse_teams(payload)


def test_parse_teams_too_few_raises():
    import pytest
    payload = _load("teams_sample.json")
    del payload["sports"][0]["leagues"][0]["teams"][0]
    with pytest.raises(ValueError, match="29"):
        snapshot_rosters.parse_teams(payload)


def test_parse_roster_fields():
    rows = snapshot_rosters.parse_roster(_load("roster_sample.json"), "LAL", TODAY)
    assert len(rows) == 3
    assert all(isinstance(r["espn_player_id"], int) for r in rows)
    assert all(r["team_abbr"] == "LAL" and r["snapshot_date_et"] == TODAY for r in rows)
    carr = next(r for r in rows if r["player_name"] == "Cameron Carr")
    assert carr["position"] == "G" and carr["jersey"] == "43" and carr["status"] == "Active"
    assert carr["injury_status"] == "Out"
    assert next(r for r in rows if r["player_name"] == "Luka Doncic")["injury_status"] is None


def test_parse_roster_skips_rows_without_player_id():
    payload = _load("roster_sample.json")
    payload["athletes"][0]["id"] = None
    assert len(snapshot_rosters.parse_roster(payload, "LAL", TODAY)) == 2


def test_roster_upsert_is_per_day():
    assert ("ON CONFLICT (snapshot_date_et, team_abbr, espn_player_id)"
            in snapshot_rosters.UPSERT_ROSTER_SQL)


def _fake_get_json(failing_team_id=None):
    teams = _load("teams_sample.json")
    roster = _load("roster_sample.json")

    def get_json(url, **kw):
        if url.endswith("/teams"):
            return teams
        tid = url.split("/teams/")[1].split("/")[0]
        if tid == failing_team_id:
            return None
        return roster

    return get_json


def test_main_writes_rosters_and_reports_failed_teams(monkeypatch):
    import pytest
    log: list = []
    written: list = []
    monkeypatch.setattr(snapshot_rosters, "get_json", _fake_get_json(failing_team_id="18"))
    monkeypatch.setattr(snapshot_rosters, "db_connect", lambda: _Conn(log))
    monkeypatch.setattr(snapshot_rosters, "ensure_schema", lambda conn: None)
    monkeypatch.setattr(snapshot_rosters.psycopg2.extras, "execute_values",
                        lambda cur, sql, rows, page_size=0: written.extend(rows))
    with pytest.raises(RuntimeError, match="NYK"):
        snapshot_rosters.main()
    # the other 29 teams are still written, with one shared captured_at
    assert len(written) == 29 * 3
    assert len({r[1] for r in written}) == 1
    assert "NYK" not in {r[2] for r in written}


def test_main_fails_when_roster_format_parses_to_nothing(monkeypatch):
    import pytest
    grouped = {"team": {"id": "13"}, "athletes": [{"position": "Guards", "items": []}]}
    teams = _load("teams_sample.json")
    monkeypatch.setattr(snapshot_rosters, "get_json",
                        lambda url, **kw: teams if url.endswith("/teams") else grouped)
    monkeypatch.setattr(snapshot_rosters, "db_connect", lambda: _Conn([]))
    monkeypatch.setattr(snapshot_rosters, "ensure_schema", lambda conn: None)
    written: list = []
    monkeypatch.setattr(snapshot_rosters.psycopg2.extras, "execute_values",
                        lambda cur, sql, rows, page_size=0: written.extend(rows))
    with pytest.raises(RuntimeError, match="LAL"):
        snapshot_rosters.main()
    assert written == []


def test_main_replaces_same_day_rows_for_fetched_teams_only(monkeypatch):
    import pytest
    log: list = []
    monkeypatch.setattr(snapshot_rosters, "get_json", _fake_get_json(failing_team_id="18"))
    monkeypatch.setattr(snapshot_rosters, "db_connect", lambda: _Conn(log))
    monkeypatch.setattr(snapshot_rosters, "ensure_schema", lambda conn: None)
    monkeypatch.setattr(snapshot_rosters.psycopg2.extras, "execute_values",
                        lambda cur, sql, rows, page_size=0: log.append(("insert", None)))
    with pytest.raises(RuntimeError):
        snapshot_rosters.main()
    deletes = [p for sql, p in log if sql.startswith("DELETE FROM public.roster_snapshots")]
    assert len(deletes) == 1
    teams = deletes[0][1]
    assert len(teams) == 29 and "NYK" not in teams
    # the delete happens before the insert
    kinds = [("delete" if str(sql).startswith("DELETE") else "insert") for sql, _ in log]
    assert kinds.index("delete") < kinds.index("insert")


def test_schema_v4_additions():
    for table in ("player_game_status", "game_officials", "team_shot_buckets"):
        assert f"CREATE TABLE IF NOT EXISTS public.{table}" in schema.DDL
    for col in ("neutral_site", "price_point", "total_price_point"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in schema.MIGRATIONS
    assert "PRIMARY KEY (game_id, player_id)" in schema.DDL
    assert "PRIMARY KEY (game_id, team_abbr, pts_value, dist_bucket, corner)" in schema.DDL
    assert "DROP" not in schema.MIGRATIONS.upper() and "DROP" not in schema.DDL.upper()


def test_new_market_line_columns_are_appended_last():
    """On a fresh DB the view columns must come out in the same order as on production."""
    last_stmt = schema.MIGRATIONS.rstrip().splitlines()[-1]
    assert "total_price_point" in last_stmt or "neutral_site" in last_stmt
    assert schema.MIGRATIONS.index("price_point") > schema.MIGRATIONS.index("total_model_version")
