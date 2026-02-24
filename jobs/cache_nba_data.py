#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
jobs/cache_nba_data.py  (HARDENED)

Cache strategy (minimal, stable):
- Cache ONE record of player stats (optional)     -> cache_key: player_stats_{season}
- Cache ONE record of team context for anchor day -> cache_key: team_ctx_{season}_{YYYYMMDD}
  where team_ctx is {team_id: {"b2b": bool, "recent_w": float}}

Why:
- TeamGameLog per team (30 calls) is the #1 cause of hangs/slowdowns.
- We compute the only things we need (b2b + last5 win%) and store compactly.

Env:
- SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD, SUPABASE_PORT (or DATABASE_URL)
- NBA_SEASON (default 2025-26)
- OVERRIDE_US_DATE (MM/DD/YYYY) optional for anchor date (US/Eastern)
- CACHE_MODE: all | players | team_ctx  (default all)

Hard limits:
- NBA_API_TIMEOUT_S (default 10)
- NBA_API_RETRIES (default 1)
- TEAM_CTX_DEADLINE_S (default 180)  total time budget for team ctx building
- TEAM_CTX_MAX_TEAMS (default 30)    cap teams processed
- TEAM_CTX_BATCH_SIZE (default 10)   process in batches with short sleeps
"""

import os
import time
import json
import datetime as dt
from typing import Optional, List, Dict, Any

import psycopg2
import psycopg2.extras
import pandas as pd

from nba_api.stats.endpoints import leaguedashplayerstats, teamgamelog
from nba_api.stats.static import teams as nba_teams


# -------------------------
# Time helpers
# -------------------------

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


def anchor_us_date() -> dt.date:
    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()
    if override:
        return dt.datetime.strptime(override, "%m/%d/%Y").date()
    return us_eastern_today()


# -------------------------
# DB
# -------------------------

def db_connect():
    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if db_url:
        return psycopg2.connect(db_url)

    host = (os.environ.get("SUPABASE_HOST") or "").strip()
    dbname = (os.environ.get("SUPABASE_DB") or "").strip()
    user = (os.environ.get("SUPABASE_USER") or "").strip()
    password = (os.environ.get("SUPABASE_PASSWORD") or "").strip()
    port = (os.environ.get("SUPABASE_PORT") or "5432").strip()

    present = all([host, dbname, user, password, port])
    print(f"[INFO] DB_ENV_present={present} via={'SUPABASE_*' if present else 'none'}")
    if not present:
        raise RuntimeError("DB env missing: set DATABASE_URL or SUPABASE_HOST/DB/USER/PASSWORD/PORT")

    return psycopg2.connect(
        host=host,
        dbname=dbname,
        user=user,
        password=password,
        port=int(port),
        sslmode="require",
    )


def ensure_cache_schema():
    ddl = """
    CREATE TABLE IF NOT EXISTS public.nba_cache (
      cache_key TEXT PRIMARY KEY,
      season TEXT,
      payload JSONB,
      pulled_at_tw TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_nba_cache_season ON public.nba_cache(season);
    """
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
        print("[INFO] nba_cache schema ensured")
    finally:
        conn.close()


UPSERT_CACHE_SQL = """
INSERT INTO public.nba_cache (cache_key, season, payload, pulled_at_tw)
VALUES (%(cache_key)s, %(season)s, %(payload)s::jsonb, %(pulled_at_tw)s)
ON CONFLICT (cache_key)
DO UPDATE SET
  season = EXCLUDED.season,
  payload = EXCLUDED.payload,
  pulled_at_tw = EXCLUDED.pulled_at_tw;
"""


def upsert_cache_rows(rows: List[dict]) -> None:
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, UPSERT_CACHE_SQL, rows, page_size=50)
        print(f"[INFO] cache upsert ok rows={len(rows)}")
    finally:
        conn.close()


# -------------------------
# nba_api wrapper
# -------------------------

NBA_API_TIMEOUT_S = int((os.environ.get("NBA_API_TIMEOUT_S") or "10").strip())
NBA_API_RETRIES = int((os.environ.get("NBA_API_RETRIES") or "1").strip())

TEAM_CTX_DEADLINE_S = int((os.environ.get("TEAM_CTX_DEADLINE_S") or "180").strip())
TEAM_CTX_MAX_TEAMS = int((os.environ.get("TEAM_CTX_MAX_TEAMS") or "30").strip())
TEAM_CTX_BATCH_SIZE = int((os.environ.get("TEAM_CTX_BATCH_SIZE") or "10").strip())


def fetch_safe_df(endpoint_cls, timeout_s: int = NBA_API_TIMEOUT_S, retries: int = NBA_API_RETRIES, sleep_s: float = 0.8, **kwargs) -> pd.DataFrame:
    kwargs = dict(kwargs)
    kwargs.setdefault("timeout", timeout_s)

    for attempt in range(retries + 1):
        try:
            t0 = time.time()
            d = endpoint_cls(**kwargs).get_dict()
            rs = d["resultSets"][0]
            df = pd.DataFrame(rs["rowSet"], columns=rs["headers"])
            print(f"[INFO] nba_api ok {endpoint_cls.__name__} took={round(time.time()-t0,2)}s")
            return df
        except Exception as e:
            print(f"[WARN] nba_api failed {endpoint_cls.__name__} attempt={attempt+1}/{retries+1} err={e}")
            if attempt < retries:
                time.sleep(sleep_s * (attempt + 1))
            else:
                return pd.DataFrame()


# -------------------------
# Cache builders
# -------------------------

def build_player_stats_cache(season: str) -> Optional[dict]:
    df = fetch_safe_df(
        leaguedashplayerstats.LeagueDashPlayerStats,
        season=season,
        per_mode_detailed="PerGame",
    )
    if df.empty:
        return None

    # Keep only needed columns to reduce payload size
    keep = ["PLAYER_NAME", "TEAM_ID", "GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV"]
    for c in keep:
        if c not in df.columns:
            df[c] = 0

    df = df[keep].copy()
    payload = {"rows": df.to_dict(orient="records")}
    return payload


def compute_team_ctx_for_date(season: str, game_day: dt.date) -> Dict[str, Any]:
    """
    ctx: team_id(str) -> {"b2b": bool, "recent_w": float}
    computed from cached TeamGameLog (pulled now) but with:
    - total deadline
    - batch processing
    """
    teams = nba_teams.get_teams()
    team_ids = [int(t["id"]) for t in teams][:TEAM_CTX_MAX_TEAMS]

    ctx: Dict[str, Any] = {}
    prev_day = game_day - dt.timedelta(days=1)

    t_start = time.time()
    for i in range(0, len(team_ids), TEAM_CTX_BATCH_SIZE):
        if time.time() - t_start > TEAM_CTX_DEADLINE_S:
            print(f"[WARN] TEAM_CTX deadline hit after {round(time.time()-t_start,1)}s -> stop early")
            break

        batch = team_ids[i:i + TEAM_CTX_BATCH_SIZE]
        print(f"[INFO] TEAM_CTX batch {i//TEAM_CTX_BATCH_SIZE+1} teams={len(batch)}")

        for tid in batch:
            if time.time() - t_start > TEAM_CTX_DEADLINE_S:
                break

            log = fetch_safe_df(teamgamelog.TeamGameLog, team_id=tid, season=season)
            is_b2b, recent_w = False, 0.5

            if not log.empty and "GAME_DATE" in log.columns and "WL" in log.columns:
                log = log.head(20).copy()
                log["GAME_DATE"] = pd.to_datetime(
                    log["GAME_DATE"],
                    format="%b %d, %Y",
                    errors="coerce"
                ).dt.date
                log = log.dropna(subset=["GAME_DATE"])

                prior = log[log["GAME_DATE"] < game_day].sort_values("GAME_DATE", ascending=False)
                if not prior.empty:
                    last_game_date = prior.iloc[0]["GAME_DATE"]
                    is_b2b = (last_game_date == prev_day)
                    last5 = prior.head(5)
                    if len(last5) > 0:
                        recent_w = (last5["WL"] == "W").mean()

            ctx[str(tid)] = {"b2b": bool(is_b2b), "recent_w": float(recent_w)}

        # small sleep between batches to reduce rate-limit risk
        time.sleep(0.6)

    print(f"[INFO] TEAM_CTX computed teams={len(ctx)} took={round(time.time()-t_start,1)}s")
    return ctx


def main():
    ensure_cache_schema()

    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()
    cache_mode = (os.environ.get("CACHE_MODE") or "all").strip().lower()
    pulled_at_tw = now_tw_str()
    anchor_day = anchor_us_date()
    key_day = anchor_day.strftime("%Y%m%d")

    print(f"[INFO] NBA_SEASON={season} CACHE_MODE={cache_mode} anchor_us={anchor_day.isoformat()}")
    print(f"[INFO] NBA_API_TIMEOUT_S={NBA_API_TIMEOUT_S} NBA_API_RETRIES={NBA_API_RETRIES}")
    print(f"[INFO] TEAM_CTX_DEADLINE_S={TEAM_CTX_DEADLINE_S} TEAM_CTX_BATCH_SIZE={TEAM_CTX_BATCH_SIZE}")

    rows: List[dict] = []

    if cache_mode in ("all", "players"):
        t0 = time.time()
        payload = build_player_stats_cache(season)
        print(f"[T] player_stats build took={round(time.time()-t0,2)}s")
        if payload:
            rows.append({
                "cache_key": f"player_stats_{season}",
                "season": season,
                "payload": json.dumps(payload, ensure_ascii=False),
                "pulled_at_tw": pulled_at_tw,
            })
        else:
            print("[WARN] player_stats cache skipped (empty)")

    if cache_mode in ("all", "team_ctx"):
        t0 = time.time()
        ctx = compute_team_ctx_for_date(season, anchor_day)
        print(f"[T] team_ctx build took={round(time.time()-t0,2)}s")
        if ctx:
            rows.append({
                "cache_key": f"team_ctx_{season}_{key_day}",
                "season": season,
                "payload": json.dumps({"ctx": ctx, "asof_us_date": anchor_day.isoformat()}, ensure_ascii=False),
                "pulled_at_tw": pulled_at_tw,
            })
        else:
            print("[WARN] team_ctx cache skipped (empty)")

    if rows:
        upsert_cache_rows(rows)

    print("[OK] cache job complete")


if __name__ == "__main__":
    main()
