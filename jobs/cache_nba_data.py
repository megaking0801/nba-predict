#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import base64
import datetime as dt
from typing import Dict, Any, Optional

import pandas as pd
import psycopg2
import psycopg2.extras

from nba_api.stats.endpoints import leaguedashplayerstats, teamgamelog
from nba_api.stats.static import teams as nba_teams


def now_tw_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Taipei")
        return dt.datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def us_eastern_today() -> dt.date:
    try:
        from zoneinfo import ZoneInfo
        now_et = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        return now_et.date()
    except Exception:
        return (dt.datetime.utcnow() - dt.timedelta(hours=5)).date()


def db_connect():
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if db_url:
        return psycopg2.connect(db_url)

    host = (os.environ.get("SUPABASE_HOST") or "").strip()
    dbname = (os.environ.get("SUPABASE_DB") or "").strip()
    user = (os.environ.get("SUPABASE_USER") or "").strip()
    password = (os.environ.get("SUPABASE_PASSWORD") or "").strip()
    port = (os.environ.get("SUPABASE_PORT") or "5432").strip()

    if not all([host, dbname, user, password, port]):
        raise RuntimeError("DB env missing: set DATABASE_URL or SUPABASE_HOST/DB/USER/PASSWORD/PORT")

    return psycopg2.connect(
        host=host, dbname=dbname, user=user, password=password, port=int(port), sslmode="require"
    )


def ensure_schema():
    ddl = """
    CREATE TABLE IF NOT EXISTS public.nba_cache (
      cache_key TEXT PRIMARY KEY,
      payload_json TEXT,
      updated_at_tw TEXT
    );

    CREATE TABLE IF NOT EXISTS public.model_registry (
      model_name TEXT PRIMARY KEY,
      model_version TEXT,
      payload_base64 TEXT,
      trained_rows INT,
      metrics JSONB,
      created_at_tw TEXT
    );

    CREATE TABLE IF NOT EXISTS public.games (
        game_id TEXT PRIMARY KEY,
        game_date_us TEXT,
        season TEXT,
        away_abbr TEXT,
        home_abbr TEXT,
        away_name TEXT,
        home_name TEXT,

        home_spread DOUBLE PRECISION,
        home_odds DOUBLE PRECISION,
        away_odds DOUBLE PRECISION,
        line_source TEXT,

        status TEXT,
        away_score INTEGER,
        home_score INTEGER,

        margin INTEGER,
        cover INTEGER,
        settled_at_tw TEXT,

        -- feature columns for base model
        home_pts_sum DOUBLE PRECISION,
        away_pts_sum DOUBLE PRECISION,
        home_impact_mean DOUBLE PRECISION,
        away_impact_mean DOUBLE PRECISION,
        home_b2b BOOLEAN,
        away_b2b BOOLEAN,
        home_recent_w DOUBLE PRECISION,
        away_recent_w DOUBLE PRECISION,

        base_diff DOUBLE PRECISION,
        f_edge DOUBLE PRECISION,
        cover_prob DOUBLE PRECISION,
        implied_prob DOUBLE PRECISION,
        edge_value DOUBLE PRECISION,
        ev DOUBLE PRECISION,
        pick_team TEXT,
        odds_used DOUBLE PRECISION,

        created_at_tw TEXT,
        updated_at_tw TEXT,
        game_date_tw TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_games_date_us ON public.games (game_date_us);
    """
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
        print("[INFO] schema ensured")
    finally:
        conn.close()


def fetch_safe_df(endpoint_cls, retries: int = 2, sleep_s: float = 0.9, **kwargs) -> pd.DataFrame:
    for attempt in range(retries + 1):
        try:
            d = endpoint_cls(**kwargs).get_dict()
            rs = d["resultSets"][0]
            return pd.DataFrame(rs["rowSet"], columns=rs["headers"])
        except Exception as e:
            if attempt < retries:
                time.sleep(sleep_s * (attempt + 1))
            else:
                print(f"[WARN] endpoint failed: {endpoint_cls.__name__} err={e}")
                return pd.DataFrame()


def cache_put(cache_key: str, payload: Dict[str, Any]) -> None:
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.nba_cache(cache_key, payload_json, updated_at_tw)
                    VALUES (%s, %s, %s)
                    ON CONFLICT(cache_key) DO UPDATE SET
                      payload_json=EXCLUDED.payload_json,
                      updated_at_tw=EXCLUDED.updated_at_tw
                    """,
                    (cache_key, json.dumps(payload, ensure_ascii=False), now_tw_str()),
                )
    finally:
        conn.close()


def main():
    ensure_schema()

    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()

    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()
    if override:
        anchor = dt.datetime.strptime(override, "%m/%d/%Y").date()
    else:
        anchor = us_eastern_today()

    backfill_past_days = int((os.environ.get("BACKFILL_PAST_DAYS") or "120").strip())
    if backfill_past_days < 1:
        backfill_past_days = 1

    # 1) player stats snapshot (season-wide, heavy but one call)
    ps = fetch_safe_df(
        leaguedashplayerstats.LeagueDashPlayerStats,
        season=season,
        per_mode_detailed="PerGame",
    )
    cache_put(f"player_stats:{season}", {"season": season, "rows": ps.to_dict(orient="records")})
    print(f"[OK] cached player_stats season={season} rows={len(ps)}")

    # 2) teamgamelog for every team (this is the heavy part)
    all_teams = nba_teams.get_teams()
    abbr_to_id = {t["abbreviation"]: int(t["id"]) for t in all_teams}

    # cache logs only once/day
    for abbr, tid in abbr_to_id.items():
        log = fetch_safe_df(teamgamelog.TeamGameLog, team_id=tid, season=season)
        cache_put(f"team_log:{season}:{abbr}", {"season": season, "abbr": abbr, "rows": log.to_dict(orient="records")})
        print(f"[OK] cached team_log {abbr} rows={len(log)}")
        time.sleep(0.35)

    # 3) store the run metadata
    cache_put("cache_meta", {
        "season": season,
        "anchor_us": anchor.strftime("%Y-%m-%d"),
        "backfill_past_days": backfill_past_days,
        "updated_at_tw": now_tw_str(),
    })
    print("[OK] cache complete")


if __name__ == "__main__":
    main()
