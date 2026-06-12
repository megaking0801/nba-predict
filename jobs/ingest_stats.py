"""Ingest team + player boxscores from NBA stats `leaguegamelog`.

One request per (season, season_type, T/P) covers every game — a full season
backfill is 6 requests, the daily refresh is 2. Re-running upserts the whole
season log, which also self-heals any previously missed day.

Run: python -m jobs.ingest_stats --season 2025-26 [--season-types regular,playin,playoffs]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd
import psycopg2.extras

from jobs.db_utils import db_connect
from jobs.nba_http import CircuitBreaker, make_http_session, nba_stats_get_json, resultset_to_df
from jobs.schema import ensure_schema
from jobs.teams import require_abbr

SEASON_TYPE_PARAM = {
    "regular": "Regular Season",
    "playin": "PlayIn",
    "playoffs": "Playoffs",
}

INTER_REQUEST_SLEEP_S = 2.5

STAT_COLS = ["FGM", "FGA", "FG3M", "FG3A", "FTM", "FTA", "OREB", "DREB",
             "REB", "AST", "STL", "BLK", "TOV", "PF", "PTS", "PLUS_MINUS"]


def _i(v) -> Optional[int]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _f(v) -> Optional[float]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_league_game_log(session, cb: CircuitBreaker, season: str,
                          season_type_key: str, player_or_team: str) -> pd.DataFrame:
    params = {
        "Counter": "0",
        "DateFrom": "",
        "DateTo": "",
        "Direction": "ASC",
        "LeagueID": "00",
        "PlayerOrTeam": player_or_team,  # 'T' or 'P'
        "Season": season,
        "SeasonType": SEASON_TYPE_PARAM[season_type_key],
        "Sorter": "DATE",
    }
    data = nba_stats_get_json(session, "leaguegamelog", params, cb=cb)
    if not data:
        return pd.DataFrame()
    return resultset_to_df(data, 0)


def build_game_rows(team_df: pd.DataFrame, season: str, season_type_key: str) -> List[tuple]:
    """Pair the two team rows per GAME_ID into one games_v2 row.

    Home side is the row whose MATCHUP contains ' vs. '.
    """
    rows: List[tuple] = []
    for game_id, grp in team_df.groupby("GAME_ID"):
        if len(grp) != 2:
            print(f"[WARN] game {game_id} has {len(grp)} team rows; skip", flush=True)
            continue
        home_row = away_row = None
        for _, r in grp.iterrows():
            if " vs. " in str(r["MATCHUP"]):
                home_row = r
            elif " @ " in str(r["MATCHUP"]):
                away_row = r
        if home_row is None or away_row is None:
            print(f"[WARN] game {game_id} matchup parse failed; skip", flush=True)
            continue
        home_abbr = require_abbr(home_row["TEAM_ABBREVIATION"])
        away_abbr = require_abbr(away_row["TEAM_ABBREVIATION"])
        home_pts = _i(home_row["PTS"])
        away_pts = _i(away_row["PTS"])
        margin = (home_pts - away_pts) if (home_pts is not None and away_pts is not None) else None
        rows.append((
            str(game_id), season, season_type_key, str(home_row["GAME_DATE"]),
            home_abbr, away_abbr, "final", home_pts, away_pts, margin,
        ))
    return rows


UPSERT_GAMES_SQL = """
INSERT INTO public.games_v2
  (game_id, season, season_type, game_date_et, home_abbr, away_abbr,
   status, home_score, away_score, margin, updated_at)
VALUES %s
ON CONFLICT (game_id) DO UPDATE SET
  season       = EXCLUDED.season,
  season_type  = EXCLUDED.season_type,
  game_date_et = EXCLUDED.game_date_et,
  home_abbr    = EXCLUDED.home_abbr,
  away_abbr    = EXCLUDED.away_abbr,
  status       = EXCLUDED.status,
  home_score   = EXCLUDED.home_score,
  away_score   = EXCLUDED.away_score,
  margin       = EXCLUDED.margin,
  updated_at   = now()
"""

UPSERT_TEAM_STATS_SQL = """
INSERT INTO public.team_game_stats
  (game_id, team_abbr, is_home, wl, min, pts, fgm, fga, fg3m, fg3a, ftm, fta,
   oreb, dreb, reb, ast, stl, blk, tov, pf, plus_minus, updated_at)
VALUES %s
ON CONFLICT (game_id, team_abbr) DO UPDATE SET
  is_home = EXCLUDED.is_home, wl = EXCLUDED.wl, min = EXCLUDED.min,
  pts = EXCLUDED.pts, fgm = EXCLUDED.fgm, fga = EXCLUDED.fga,
  fg3m = EXCLUDED.fg3m, fg3a = EXCLUDED.fg3a, ftm = EXCLUDED.ftm,
  fta = EXCLUDED.fta, oreb = EXCLUDED.oreb, dreb = EXCLUDED.dreb,
  reb = EXCLUDED.reb, ast = EXCLUDED.ast, stl = EXCLUDED.stl,
  blk = EXCLUDED.blk, tov = EXCLUDED.tov, pf = EXCLUDED.pf,
  plus_minus = EXCLUDED.plus_minus, updated_at = now()
"""

UPSERT_PLAYER_STATS_SQL = """
INSERT INTO public.player_game_stats
  (game_id, player_id, team_abbr, player_name, min_played, pts, fgm, fga,
   fg3m, fg3a, ftm, fta, oreb, dreb, reb, ast, stl, blk, tov, pf,
   plus_minus, updated_at)
VALUES %s
ON CONFLICT (game_id, player_id) DO UPDATE SET
  team_abbr = EXCLUDED.team_abbr, player_name = EXCLUDED.player_name,
  min_played = EXCLUDED.min_played, pts = EXCLUDED.pts, fgm = EXCLUDED.fgm,
  fga = EXCLUDED.fga, fg3m = EXCLUDED.fg3m, fg3a = EXCLUDED.fg3a,
  ftm = EXCLUDED.ftm, fta = EXCLUDED.fta, oreb = EXCLUDED.oreb,
  dreb = EXCLUDED.dreb, reb = EXCLUDED.reb, ast = EXCLUDED.ast,
  stl = EXCLUDED.stl, blk = EXCLUDED.blk, tov = EXCLUDED.tov,
  pf = EXCLUDED.pf, plus_minus = EXCLUDED.plus_minus, updated_at = now()
"""


def build_team_stat_rows(team_df: pd.DataFrame) -> List[tuple]:
    rows = []
    for _, r in team_df.iterrows():
        rows.append((
            str(r["GAME_ID"]), require_abbr(r["TEAM_ABBREVIATION"]),
            " vs. " in str(r["MATCHUP"]), r.get("WL"), _i(r.get("MIN")),
            *(_i(r.get(c)) for c in STAT_COLS),
        ))
    return rows


def build_player_stat_rows(player_df: pd.DataFrame) -> List[tuple]:
    rows = []
    for _, r in player_df.iterrows():
        pid = _i(r.get("PLAYER_ID"))
        if pid is None:
            continue
        rows.append((
            str(r["GAME_ID"]), pid, require_abbr(r["TEAM_ABBREVIATION"]),
            r.get("PLAYER_NAME"), _f(r.get("MIN")),
            *(_i(r.get(c)) for c in STAT_COLS),
        ))
    return rows


def ingest_season_type(conn, session, cb: CircuitBreaker, season: str,
                       season_type_key: str) -> Tuple[int, int, int]:
    """Returns (games, team_rows, player_rows) upserted."""
    team_df = fetch_league_game_log(session, cb, season, season_type_key, "T")
    time.sleep(INTER_REQUEST_SLEEP_S)
    player_df = fetch_league_game_log(session, cb, season, season_type_key, "P")
    time.sleep(INTER_REQUEST_SLEEP_S)

    if team_df.empty:
        print(f"[WARN] no team rows for {season} {season_type_key}", flush=True)
        return (0, 0, 0)

    game_rows = build_game_rows(team_df, season, season_type_key)
    team_rows = build_team_stat_rows(team_df)
    player_rows = build_player_stat_rows(player_df) if not player_df.empty else []

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, UPSERT_GAMES_SQL, game_rows, page_size=500)
        psycopg2.extras.execute_values(cur, UPSERT_TEAM_STATS_SQL, team_rows, page_size=500)
        if player_rows:
            psycopg2.extras.execute_values(cur, UPSERT_PLAYER_STATS_SQL, player_rows, page_size=500)
    conn.commit()
    print(f"[OK] {season} {season_type_key}: games={len(game_rows)} "
          f"team_rows={len(team_rows)} player_rows={len(player_rows)}", flush=True)
    return (len(game_rows), len(team_rows), len(player_rows))


def ingest_season(conn, session, cb: CircuitBreaker, season: str,
                  season_type_keys: List[str]) -> Dict[str, Tuple[int, int, int]]:
    out = {}
    for stk in season_type_keys:
        out[stk] = ingest_season_type(conn, session, cb, season, stk)
    return out


def parse_season_types(s: str) -> List[str]:
    keys = [k.strip().lower() for k in s.split(",") if k.strip()]
    for k in keys:
        if k not in SEASON_TYPE_PARAM:
            raise SystemExit(f"unknown season type: {k} (valid: {list(SEASON_TYPE_PARAM)})")
    return keys


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default=os.environ.get("NBA_SEASON") or "2025-26")
    ap.add_argument("--season-types", default="regular,playin,playoffs")
    args = ap.parse_args()

    season_types = parse_season_types(args.season_types)
    conn = db_connect()
    session = make_http_session()
    cb = CircuitBreaker(fail_threshold=int(os.environ.get("NBA_STATS_CB_THRESHOLD") or "6"))
    try:
        ensure_schema(conn)
        ingest_season(conn, session, cb, args.season, season_types)
    finally:
        conn.close()
    if not cb.allow():
        print("[ERROR] circuit breaker opened during ingest", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
