"""Snapshot the injury report from ESPN's JSON endpoint (replaces the old
brittle HTML scrape). injury_snapshots keeps one row per (day, team, player) and
upserts; injury_reports appends every capture (with ESPN's own report time).

Run: python -m jobs.snapshot_injuries
"""
from __future__ import annotations

import datetime as dt
import re
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

INSERT_REPORT_SQL = """
INSERT INTO public.injury_reports
  (captured_at, reported_at, snapshot_date_et, team_abbr, player_name,
   espn_player_id, status, detail, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'espn')
"""


_LINK_ID = re.compile(r"/id/(\d+)(?:/|$)")


def _player_id(athlete: dict) -> Optional[int]:
    """ESPN's injuries payload has no athlete.id (the entry's own "id" is the report's, not the
    player's); the player id only appears in the athlete's links, e.g. .../player/_/id/4397183/name.
    Same id space as the roster endpoint's athlete id."""
    try:
        return int(athlete.get("id"))
    except (TypeError, ValueError):
        pass
    links = athlete.get("links") or []
    # the player card is the canonical link; fall back to any other link carrying /id/<n>/
    for link in sorted(links, key=lambda l: "playercard" not in (l.get("rel") or [])):
        m = _LINK_ID.search(str(link.get("href") or ""))
        if m:
            return int(m.group(1))
    return None


def _reported_at(raw) -> Optional[dt.datetime]:
    """ESPN '2026-10-19T19:38Z' -> aware UTC datetime; anything odd -> None."""
    try:
        ts = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def parse_injuries(payload: dict, today: dt.date) -> List[dict]:
    rows: List[dict] = []
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
            rows.append({
                "snapshot_date_et": today, "team_abbr": abbr, "player_name": name,
                "espn_player_id": _player_id(athlete), "status": status,
                "detail": (detail or "")[:500] or None,
                "reported_at": _reported_at(inj.get("date")),
            })
    return rows


def fetch_injury_rows() -> List[dict]:
    r = requests.get(INJURIES_URL, timeout=25)
    r.raise_for_status()
    return parse_injuries(r.json(), et_today())


def main() -> None:
    rows = fetch_injury_rows()
    captured_at = dt.datetime.now(dt.timezone.utc)
    conn = db_connect()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(UPSERT_SQL, (r["snapshot_date_et"], r["team_abbr"], r["player_name"],
                                         r["espn_player_id"], r["status"], r["detail"]))
                cur.execute(INSERT_REPORT_SQL, (captured_at, r["reported_at"], r["snapshot_date_et"],
                                                r["team_abbr"], r["player_name"],
                                                r["espn_player_id"], r["status"], r["detail"]))
        conn.commit()
    finally:
        conn.close()
    print(f"[OK] snapshot_injuries upserted {len(rows)} rows for {et_today()} "
          f"(+{len(rows)} appended to injury_reports)", flush=True)


if __name__ == "__main__":
    main()
