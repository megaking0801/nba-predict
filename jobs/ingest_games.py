"""Ingest the current season's schedule/results from the NBA CDN static
schedule file — one request covers the whole season including tipoff times,
live status, and final scores.

Run: python -m jobs.ingest_games
"""
from __future__ import annotations

import datetime as dt
import sys
from typing import List, Optional

import psycopg2.extras
import requests

from jobs.db_utils import db_connect
from jobs.schema import ensure_schema
from jobs.teams import CANONICAL_ABBRS
from jobs.tz import et_date_of

SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"

# gameId prefix -> season_type; anything absent (preseason 001, all-star 003,
# NBA Cup final 006) is skipped because it has no place in the training data.
GAME_ID_PREFIX_TO_TYPE = {"002": "regular", "004": "playoffs", "005": "playin"}

STATUS_MAP = {1: "scheduled", 2: "live", 3: "final"}

# Never let a stale schedule row regress a final game back to scheduled/live.
UPSERT_SQL = """
INSERT INTO public.games_v2
  (game_id, season, season_type, game_date_et, tipoff_utc, home_abbr, away_abbr,
   status, home_score, away_score, margin, updated_at)
VALUES %s
ON CONFLICT (game_id) DO UPDATE SET
  season       = EXCLUDED.season,
  season_type  = EXCLUDED.season_type,
  game_date_et = EXCLUDED.game_date_et,
  tipoff_utc   = COALESCE(EXCLUDED.tipoff_utc, games_v2.tipoff_utc),
  home_abbr    = EXCLUDED.home_abbr,
  away_abbr    = EXCLUDED.away_abbr,
  status       = CASE WHEN games_v2.status = 'final' AND EXCLUDED.status <> 'final'
                      THEN games_v2.status ELSE EXCLUDED.status END,
  home_score   = CASE WHEN games_v2.status = 'final' AND EXCLUDED.status <> 'final'
                      THEN games_v2.home_score ELSE EXCLUDED.home_score END,
  away_score   = CASE WHEN games_v2.status = 'final' AND EXCLUDED.status <> 'final'
                      THEN games_v2.away_score ELSE EXCLUDED.away_score END,
  margin       = CASE WHEN games_v2.status = 'final' AND EXCLUDED.status <> 'final'
                      THEN games_v2.margin ELSE EXCLUDED.margin END,
  updated_at   = now()
"""


def fetch_schedule(timeout: float = 30.0) -> dict:
    r = requests.get(SCHEDULE_URL, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _parse_tipoff(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_rows(payload: dict) -> List[tuple]:
    sched = payload.get("leagueSchedule") or {}
    season = (sched.get("seasonYear") or "").strip()
    if not season:
        raise RuntimeError("schedule payload missing leagueSchedule.seasonYear")

    rows: List[tuple] = []
    skipped_type = skipped_tbd = 0
    for gd in sched.get("gameDates") or []:
        for g in gd.get("games") or []:
            game_id = str(g.get("gameId") or "")
            season_type = GAME_ID_PREFIX_TO_TYPE.get(game_id[:3])
            if season_type is None:
                skipped_type += 1
                continue
            home = g.get("homeTeam") or {}
            away = g.get("awayTeam") or {}
            home_abbr = (home.get("teamTricode") or "").strip().upper()
            away_abbr = (away.get("teamTricode") or "").strip().upper()
            if home_abbr not in CANONICAL_ABBRS or away_abbr not in CANONICAL_ABBRS:
                skipped_tbd += 1  # playoff placeholders etc.
                continue

            tipoff = _parse_tipoff(g.get("gameDateTimeUTC"))
            status = STATUS_MAP.get(g.get("gameStatus"), "scheduled")
            if tipoff is not None:
                game_date_et = et_date_of(tipoff)
            else:
                # fall back to the schedule's EST date string "MM/DD/YYYY 00:00:00"
                raw = (g.get("gameDateEst") or gd.get("gameDate") or "").split(" ")[0]
                game_date_et = dt.datetime.strptime(raw, "%m/%d/%Y").date()

            home_score = away_score = margin = None
            if status in ("live", "final"):
                hs, as_ = home.get("score"), away.get("score")
                if isinstance(hs, (int, float)) and isinstance(as_, (int, float)):
                    home_score, away_score = int(hs), int(as_)
                    margin = home_score - away_score

            rows.append((game_id, season, season_type, game_date_et, tipoff,
                         home_abbr, away_abbr, status, home_score, away_score, margin))

    print(f"[INFO] schedule {season}: rows={len(rows)} "
          f"skipped_non_competitive={skipped_type} skipped_tbd={skipped_tbd}", flush=True)
    return rows


def main() -> None:
    payload = fetch_schedule()
    rows = build_rows(payload)
    if not rows:
        print("[ERROR] schedule produced 0 rows", flush=True)
        sys.exit(1)
    conn = db_connect()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, UPSERT_SQL, rows, page_size=500)
        conn.commit()
        print(f"[OK] ingest_games upserted {len(rows)} rows", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
