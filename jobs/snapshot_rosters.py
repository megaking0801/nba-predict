"""Snapshot every team's roster from ESPN once per ET day (re-runs the same day
upsert). Lets us see trades and signings after the fact, which can't be
backfilled later.

ESPN's team ids are not NBA ids, so they are fetched from /teams each run and
mapped back to our canonical abbreviations (never hard-coded).

Run: python -m jobs.snapshot_rosters
"""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import psycopg2.extras

from jobs.db_utils import db_connect
from jobs.espn_http import ESPN_BASE, get_json
from jobs.schema import ensure_schema
from jobs.teams import CANONICAL_ABBRS, normalize_espn_abbr
from jobs.tz import et_today

MAX_WORKERS = 8

DELETE_TEAMS_SQL = """
DELETE FROM public.roster_snapshots
WHERE snapshot_date_et = %s AND team_abbr = ANY(%s)
"""

UPSERT_ROSTER_SQL = """
INSERT INTO public.roster_snapshots
  (snapshot_date_et, captured_at, team_abbr, espn_player_id, player_name,
   position, jersey, status, injury_status)
VALUES %s
ON CONFLICT (snapshot_date_et, team_abbr, espn_player_id) DO UPDATE SET
  captured_at   = EXCLUDED.captured_at,
  player_name   = EXCLUDED.player_name,
  position      = EXCLUDED.position,
  jersey        = EXCLUDED.jersey,
  status        = EXCLUDED.status,
  injury_status = EXCLUDED.injury_status
"""


def parse_teams(payload: dict) -> Dict[str, str]:
    """/teams payload -> {canonical abbr: ESPN team id}. Unknown abbr or a
    short list raises, so a rename can never silently drop a team."""
    ids: Dict[str, str] = {}
    leagues = ((payload.get("sports") or [{}])[0]).get("leagues") or [{}]
    for entry in leagues[0].get("teams") or []:
        team = entry.get("team") or {}
        ids[normalize_espn_abbr(team.get("abbreviation"))] = str(team.get("id"))
    if len(ids) != len(CANONICAL_ABBRS):
        raise ValueError(f"expected {len(CANONICAL_ABBRS)} teams from ESPN, got {len(ids)}")
    return ids


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_roster(payload: dict, team_abbr: str, today: dt.date) -> List[dict]:
    rows: List[dict] = []
    for a in payload.get("athletes") or []:
        pid = _int(a.get("id"))
        name = (a.get("displayName") or a.get("fullName") or "").strip()
        if pid is None or not name:
            continue
        injuries = a.get("injuries") or []
        rows.append({
            "snapshot_date_et": today, "team_abbr": team_abbr, "espn_player_id": pid,
            "player_name": name,
            "position": (a.get("position") or {}).get("abbreviation"),
            "jersey": a.get("jersey"),
            "status": (a.get("status") or {}).get("name"),
            "injury_status": (injuries[0].get("status") if injuries else None),
        })
    return rows


def main() -> None:
    teams_payload = get_json(f"{ESPN_BASE}/teams")
    if not teams_payload:
        raise RuntimeError("ESPN /teams fetch failed; no rosters snapshotted")
    ids = parse_teams(teams_payload)
    today, captured_at = et_today(), dt.datetime.now(dt.timezone.utc)

    abbrs = sorted(ids)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        payloads = list(ex.map(lambda a: get_json(f"{ESPN_BASE}/teams/{ids[a]}/roster"), abbrs))

    failed: List[str] = []
    rows: Dict[tuple, tuple] = {}
    for abbr, payload in zip(abbrs, payloads):
        if not payload:
            failed.append(abbr)
            continue
        parsed = parse_roster(payload, abbr, today)
        if not parsed:  # a format change must not look like a successful empty roster
            failed.append(abbr)
            continue
        for r in parsed:
            rows[(r["snapshot_date_et"], abbr, r["espn_player_id"])] = (
                r["snapshot_date_et"], captured_at, abbr, r["espn_player_id"], r["player_name"],
                r["position"], r["jersey"], r["status"], r["injury_status"])

    conn = db_connect()
    try:
        ensure_schema(conn)
        if rows:
            fetched = sorted({key[1] for key in rows})
            with conn.cursor() as cur:
                # A re-run on the same ET day replaces each fetched team's roster, so a
                # player traded in between does not stay listed on the old team.
                cur.execute(DELETE_TEAMS_SQL, (today, fetched))
                psycopg2.extras.execute_values(cur, UPSERT_ROSTER_SQL, list(rows.values()),
                                               page_size=500)
        conn.commit()
    finally:
        conn.close()
    print(f"[OK] snapshot_rosters upserted {len(rows)} rows for {today}", flush=True)
    if failed:
        raise RuntimeError(f"roster fetch failed for teams: {', '.join(failed)}")


if __name__ == "__main__":
    main()
