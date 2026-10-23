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
    assert schema.SCHEMA_VERSION == "3"
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
