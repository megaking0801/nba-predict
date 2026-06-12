"""Snapshot today's injury report from ESPN's JSON endpoint (replaces the old
brittle HTML scrape). One row per (day, team, player); re-runs upsert.

Run: python -m jobs.snapshot_injuries
"""
from __future__ import annotations

import sys
from typing import List, Optional

import requests

from jobs.db_utils import db_connect
from jobs.schema import ensure_schema
from jobs.teams import abbr_from_team_name
from jobs.tz import et_today

INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"

UPSERT_SQL = """
INSERT INTO public.injury_snapshots
  (snapshot_date_et, team_abbr, player_name, espn_player_id, status, detail, source)
VALUES (%s, %s, %s, %s, %s, %s, 'espn')
ON CONFLICT (snapshot_date_et, source, team_abbr, player_name) DO UPDATE SET
  espn_player_id = EXCLUDED.espn_player_id,
  status         = EXCLUDED.status,
  detail         = EXCLUDED.detail,
  captured_at    = now()
"""


def _player_id(athlete: dict) -> Optional[int]:
    try:
        return int(athlete.get("id"))
    except (TypeError, ValueError):
        return None


def fetch_injury_rows() -> List[tuple]:
    r = requests.get(INJURIES_URL, timeout=25)
    r.raise_for_status()
    payload = r.json()

    today = et_today()
    rows: List[tuple] = []
    for team_entry in payload.get("injuries") or []:
        team_name = team_entry.get("displayName") or ""
        abbr = abbr_from_team_name(team_name)
        if not abbr:
            print(f"[WARN] unknown injury team name: {team_name!r}", flush=True)
            continue
        for inj in team_entry.get("injuries") or []:
            athlete = inj.get("athlete") or {}
            name = (athlete.get("displayName") or "").strip()
            if not name:
                continue
            status = (inj.get("status") or "").strip() or None
            details = inj.get("details") or {}
            detail = (details.get("detail") or details.get("type")
                      or inj.get("shortComment") or "")
            rows.append((today, abbr, name, _player_id(athlete), status,
                         (detail or "")[:500] or None))
    return rows


def main() -> None:
    rows = fetch_injury_rows()
    conn = db_connect()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(UPSERT_SQL, row)
        conn.commit()
    finally:
        conn.close()
    print(f"[OK] snapshot_injuries upserted {len(rows)} rows for {et_today()}", flush=True)


if __name__ == "__main__":
    main()
