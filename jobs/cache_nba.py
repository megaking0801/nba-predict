#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import datetime as dt
from typing import Dict, Any

import pandas as pd
import psycopg2

from nba_api.stats.endpoints import leaguedashplayerstats, teamgamelog
from nba_api.stats.static import teams as nba_teams


def now_tw_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Taipei")
        return dt.datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


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
    """
    Important: table may already exist with different columns.
    We CREATE IF NOT EXISTS then ALTER to add missing columns.
    """
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                CREATE TABLE IF NOT EXISTS public.nba_cache (
                  cache_key TEXT PRIMARY KEY
                );
                """)
                # Add columns defensively
                cur.execute("ALTER TABLE public.nba_cache ADD COLUMN IF NOT EXISTS payload_json TEXT;")
                cur.execute("ALTER TABLE public.nba_cache ADD COLUMN IF NOT EXISTS updated_at_tw TEXT;")
        print("[INFO] schema ensured: nba_cache (columns enforced)")
    finally:
        conn.close()


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


def fetch_safe_df(endpoint_cls, retries: int = 4, base_sleep: float = 1.0, **kwargs) -> pd.DataFrame:
    """
    nba_api will hit stats.nba.com and can timeout or 403.
    We: (1) increase timeout, (2) set headers, (3) retry with backoff.
    """
    # These headers matter a lot for stats.nba.com stability
    headers = {
        "Host": "stats.nba.com",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nba.com",
        "Referer": "https://www.nba.com/",
        "Connection": "keep-alive",
    }

    for attempt in range(retries + 1):
        try:
            ep = endpoint_cls(timeout=60, headers=headers, **kwargs)  # <- increase timeout
            d = ep.get_dict()
            rs = d["resultSets"][0]
            return pd.DataFrame(rs["rowSet"], columns=rs["headers"])
        except Exception as e:
            if attempt < retries:
                sleep_s = base_sleep * (attempt + 1) * (attempt + 1)
                print(f"[WARN] endpoint retry {attempt+1}/{retries} {endpoint_cls.__name__} err={e} sleep={sleep_s:.1f}s")
                time.sleep(sleep_s)
            else:
                print(f"[WARN] endpoint failed: {endpoint_cls.__name__} err={e}")
                return pd.DataFrame()


def main():
    ensure_schema()

    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()

    # 1) Player stats snapshot (season-wide)
    ps = fetch_safe_df(
        leaguedashplayerstats.LeagueDashPlayerStats,
        season=season,
        per_mode_detailed="PerGame",
    )

    # even if empty, write it so sync can proceed without crashing
    cache_put(f"player_stats:{season}", {"season": season, "rows": ps.to_dict(orient="records")})
    print(f"[OK] cached player_stats season={season} rows={len(ps)}")

    # 2) Team game logs for every team
    all_teams = nba_teams.get_teams()
    abbr_to_id = {t["abbreviation"]: int(t["id"]) for t in all_teams}

    for abbr, tid in abbr_to_id.items():
        log = fetch_safe_df(teamgamelog.TeamGameLog, team_id=tid, season=season)
        cache_put(f"team_log:{season}:{abbr}", {"season": season, "abbr": abbr, "rows": log.to_dict(orient="records")})
        print(f"[OK] cached team_log {abbr} rows={len(log)}")
        time.sleep(0.6)  # be nice to stats.nba.com

    cache_put("cache_meta", {
        "season": season,
        "updated_at_tw": now_tw_str(),
    })
    print("[OK] cache complete")


if __name__ == "__main__":
    main()
