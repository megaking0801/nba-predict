#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
jobs/cache_nba_data.py

Goal:
- Pull nba_api data on a schedule and store it in Supabase table public.nba_cache
- sync_daily.py will read from this cache and will NOT hit nba_api in normal runs

Cache keys:
- player_stats_{season}
- team_gamelog_{season}_{team_id}

Env:
- SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD, SUPABASE_PORT
- NBA_SEASON (e.g., 2025-26)

Optional tuning:
- NBA_API_TIMEOUT_S (default 12)
- NBA_API_RETRIES (default 2)
- CACHE_MODE: "all" | "players" | "teams" (default "all")
"""

import os
import time
import json
import datetime as dt
from typing import Dict, Tuple, Any, Optional, List

import psycopg2
import psycopg2.extras

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
                psycopg2.extras.execute_batch(cur, UPSERT_CACHE_SQL, rows, page_size=100)
        print(f"[INFO] cache upsert ok rows={len(rows)}")
    finally:
        conn.close()


# -------------------------
# nba_api with hard timeout
# -------------------------

NBA_API_TIMEOUT_S = int((os.environ.get("NBA_API_TIMEOUT_S") or "12").strip())
NBA_API_RETRIES = int((os.environ.get("NBA_API_RETRIES") or "2").strip())


def fetch_safe_dict(endpoint_cls, timeout_s: int = NBA_API_TIMEOUT_S, retries: int = NBA_API_RETRIES, sleep_s: float = 0.9, **kwargs) -> Optional[dict]:
    kwargs = dict(kwargs)
    kwargs.setdefault("timeout", timeout_s)

    for attempt in range(retries + 1):
        try:
            t0 = time.time()
            d = endpoint_cls(**kwargs).get_dict()
            print(f"[INFO] nba_api ok {endpoint_cls.__name__} took={round(time.time()-t0,2)}s")
            return d
        except Exception as e:
            print(f"[WARN] nba_api failed {endpoint_cls.__name__} attempt={attempt+1}/{retries+1} err={e}")
            if attempt < retries:
                time.sleep(sleep_s * (attempt + 1))
            else:
                return None


# -------------------------
# Cache builders
# -------------------------

def cache_player_stats(season: str, pulled_at_tw: str) -> Optional[dict]:
    d = fetch_safe_dict(
        leaguedashplayerstats.LeagueDashPlayerStats,
        season=season,
        per_mode_detailed="PerGame",
    )
    if not d:
        return None
    # store full dict so you can evolve feature logic without refetching
    return {
        "cache_key": f"player_stats_{season}",
        "season": season,
        "payload": json.dumps(d, ensure_ascii=False),
        "pulled_at_tw": pulled_at_tw,
    }


def cache_team_gamelogs(season: str, pulled_at_tw: str) -> List[dict]:
    teams = nba_teams.get_teams()
    team_ids = [int(t["id"]) for t in teams]

    out: List[dict] = []
    for tid in team_ids:
        d = fetch_safe_dict(teamgamelog.TeamGameLog, team_id=tid, season=season)
        if not d:
            continue
        out.append({
            "cache_key": f"team_gamelog_{season}_{tid}",
            "season": season,
            "payload": json.dumps(d, ensure_ascii=False),
            "pulled_at_tw": pulled_at_tw,
        })

    return out


def main():
    ensure_cache_schema()

    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()
    cache_mode = (os.environ.get("CACHE_MODE") or "all").strip().lower()
    pulled_at_tw = now_tw_str()

    print(f"[INFO] NBA_SEASON={season} CACHE_MODE={cache_mode}")
    print(f"[INFO] NBA_API_TIMEOUT_S={NBA_API_TIMEOUT_S} NBA_API_RETRIES={NBA_API_RETRIES}")

    rows: List[dict] = []

    if cache_mode in ("all", "players"):
        r = cache_player_stats(season=season, pulled_at_tw=pulled_at_tw)
        if r:
            rows.append(r)
        else:
            print("[WARN] player_stats cache failed")

    if cache_mode in ("all", "teams"):
        team_rows = cache_team_gamelogs(season=season, pulled_at_tw=pulled_at_tw)
        print(f"[INFO] team_gamelog cached rows={len(team_rows)}")
        rows.extend(team_rows)

    if rows:
        upsert_cache_rows(rows)

    print("[OK] cache job complete")


if __name__ == "__main__":
    main()
