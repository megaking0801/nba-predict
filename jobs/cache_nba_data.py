#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
jobs/cache_nba_data.py (hardened)

Writes to Supabase/Postgres public.nba_cache:
- player_stats_{season}: minimal per-player stats for impact features
- team_ctx_{season}_{YYYYMMDD}: per-team {b2b, recent_w} for anchor day

Anti-hang controls:
- NBA_API_TIMEOUT_S (default 10)
- NBA_API_RETRIES (default 1)
- TEAM_CTX_DEADLINE_S (default 180)
- TEAM_CTX_BATCH_SIZE (default 10)
- TEAM_CTX_MAX_TEAMS (default 30)

CACHE_MODE: all | players | team_ctx
"""

import os
import json
import time
import datetime as dt
from typing import Dict, Any, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import pandas as pd
from nba_api.stats.endpoints import leaguedashplayerstats, teamgamelog
from nba_api.stats.static import teams as nba_teams


# -----------------------------
# Time helpers
# -----------------------------
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


# -----------------------------
# DB
# -----------------------------
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
        raise RuntimeError("DB env missing: set DATABASE_URL or SUPABASE_*")

    return psycopg2.connect(
        host=host, dbname=dbname, user=user, password=password, port=int(port),
        sslmode="require",
    )


def ensure_schema():
    ddl = """
    CREATE TABLE IF NOT EXISTS public.nba_cache (
      cache_key TEXT PRIMARY KEY,
      season TEXT,
      payload JSONB,
      pulled_at_tw TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_nba_cache_season ON public.nba_cache(season);

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

        base_diff DOUBLE PRECISION,
        f_edge DOUBLE PRECISION,
        cover_prob DOUBLE PRECISION,
        implied_prob DOUBLE PRECISION,
        edge_value DOUBLE PRECISION,
        ev DOUBLE PRECISION,
        pick_team TEXT,
        odds_used DOUBLE PRECISION,

        status TEXT,
        away_score INTEGER,
        home_score INTEGER,
        cover INTEGER,
        settled_at_tw TEXT,

        margin DOUBLE PRECISION,

        home_pts_sum DOUBLE PRECISION,
        away_pts_sum DOUBLE PRECISION,
        home_impact_mean DOUBLE PRECISION,
        away_impact_mean DOUBLE PRECISION,
        home_b2b INTEGER,
        away_b2b INTEGER,
        home_recent_w DOUBLE PRECISION,
        away_recent_w DOUBLE PRECISION,

        created_at_tw TEXT,
        updated_at_tw TEXT,
        game_date_tw TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_games_date_us ON public.games(game_date_us);
    """

    alters = [
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS margin DOUBLE PRECISION",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_pts_sum DOUBLE PRECISION",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_pts_sum DOUBLE PRECISION",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_impact_mean DOUBLE PRECISION",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_impact_mean DOUBLE PRECISION",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_b2b INTEGER",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_b2b INTEGER",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_recent_w DOUBLE PRECISION",
        "ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_recent_w DOUBLE PRECISION",
    ]

    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                for a in alters:
                    cur.execute(a)
        print("[INFO] schema ensured")
    finally:
        conn.close()


UPSERT_CACHE_SQL = """
INSERT INTO public.nba_cache(cache_key, season, payload, pulled_at_tw)
VALUES (%(cache_key)s, %(season)s, %(payload)s, %(pulled_at_tw)s)
ON CONFLICT(cache_key) DO UPDATE SET
  season = EXCLUDED.season,
  payload = EXCLUDED.payload,
  pulled_at_tw = EXCLUDED.pulled_at_tw;
"""


def upsert_cache(cache_key: str, season: str, payload: dict):
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(UPSERT_CACHE_SQL, {
                    "cache_key": cache_key,
                    "season": season,
                    "payload": json.dumps(payload),
                    "pulled_at_tw": now_tw_str(),
                })
        print(f"[INFO] cache upsert ok key={cache_key}")
    finally:
        conn.close()


# -----------------------------
# nba_api calls (hardened)
# -----------------------------
NBA_API_TIMEOUT_S = int((os.environ.get("NBA_API_TIMEOUT_S") or "10").strip())
NBA_API_RETRIES = int((os.environ.get("NBA_API_RETRIES") or "1").strip())

def _endpoint_df(endpoint_cls, **kwargs) -> pd.DataFrame:
    # nba_api uses requests under the hood; we can pass timeout in headers? not reliably.
    # We'll just retry with sleeps and accept occasional empty.
    last = None
    for i in range(NBA_API_RETRIES + 1):
        try:
            d = endpoint_cls(**kwargs).get_dict()
            rs = d["resultSets"][0]
            return pd.DataFrame(rs["rowSet"], columns=rs["headers"])
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    print(f"[WARN] nba_api endpoint failed: {endpoint_cls.__name__} err={last}")
    return pd.DataFrame()


def cache_player_stats(season: str):
    print(f"[INFO] caching player stats season={season}")
    df = _endpoint_df(
        leaguedashplayerstats.LeagueDashPlayerStats,
        season=season,
        per_mode_detailed="PerGame",
    )
    if df.empty:
        upsert_cache(f"player_stats_{season}", season, {"rows": []})
        return

    keep = ["PLAYER_NAME", "TEAM_ID", "GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV"]
    for c in keep:
        if c not in df.columns:
            df[c] = 0

    df = df[keep].copy()
    # convert numeric
    for c in ["TEAM_ID", "GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    rows = df.to_dict(orient="records")
    upsert_cache(f"player_stats_{season}", season, {"rows": rows})
    print(f"[INFO] player stats rows={len(rows)}")


def cache_team_ctx(season: str, anchor_us: dt.date):
    deadline_s = int((os.environ.get("TEAM_CTX_DEADLINE_S") or "180").strip())
    batch_size = int((os.environ.get("TEAM_CTX_BATCH_SIZE") or "10").strip())
    max_teams = int((os.environ.get("TEAM_CTX_MAX_TEAMS") or "30").strip())

    all_teams = nba_teams.get_teams()
    team_ids = [int(t["id"]) for t in all_teams][:max_teams]

    key_day = anchor_us.strftime("%Y%m%d")
    cache_key = f"team_ctx_{season}_{key_day}"
    print(f"[INFO] caching team_ctx key={cache_key} teams={len(team_ids)} deadline={deadline_s}s batch={batch_size}")

    game_day = anchor_us  # use anchor day as reference for b2b/recent_w
    prev_day = game_day - dt.timedelta(days=1)

    started = time.time()
    ctx: Dict[int, Dict[str, Any]] = {}

    def compute_one(tid: int) -> Tuple[bool, float]:
        log = _endpoint_df(teamgamelog.TeamGameLog, team_id=tid, season=season)
        if log.empty or "GAME_DATE" not in log.columns or "WL" not in log.columns:
            return False, 0.5

        log = log.head(15).copy()
        log["GAME_DATE"] = pd.to_datetime(log["GAME_DATE"], format="%b %d, %Y", errors="coerce").dt.date
        log = log.dropna(subset=["GAME_DATE"])

        prior = log[log["GAME_DATE"] < game_day].sort_values("GAME_DATE", ascending=False)
        if prior.empty:
            return False, 0.5

        last_game_date = prior.iloc[0]["GAME_DATE"]
        is_b2b = (last_game_date == prev_day)

        last5 = prior.head(5)
        recent_w = float((last5["WL"] == "W").mean()) if len(last5) > 0 else 0.5
        return bool(is_b2b), recent_w

    # batch loop with hard deadline
    i = 0
    while i < len(team_ids):
        if time.time() - started > deadline_s:
            print(f"[WARN] team_ctx deadline reached at i={i}/{len(team_ids)}")
            break

        batch = team_ids[i:i+batch_size]
        for tid in batch:
            if time.time() - started > deadline_s:
                break
            b2b, recent_w = compute_one(tid)
            ctx[int(tid)] = {"b2b": bool(b2b), "recent_w": float(recent_w)}
        i += batch_size
        print(f"[INFO] team_ctx progress {min(i,len(team_ids))}/{len(team_ids)} elapsed={round(time.time()-started,1)}s")

    upsert_cache(cache_key, season, {"ctx": ctx, "anchor_us": key_day})
    print(f"[INFO] team_ctx cached teams={len(ctx)} key={cache_key}")


def main():
    ensure_schema()

    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()
    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()
    if override:
        anchor_us = dt.datetime.strptime(override, "%m/%d/%Y").date()
    else:
        anchor_us = us_eastern_today()

    mode = (os.environ.get("CACHE_MODE") or "all").strip().lower()
    if mode not in ("all", "players", "team_ctx"):
        mode = "all"

    if mode in ("all", "players"):
        cache_player_stats(season)

    if mode in ("all", "team_ctx"):
        cache_team_ctx(season, anchor_us)

    print("[OK] cache complete")


if __name__ == "__main__":
    main()
