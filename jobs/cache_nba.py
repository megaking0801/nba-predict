#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import datetime as dt
from typing import Dict, Any, List, Set, Optional

import requests
import pandas as pd
import psycopg2

from nba_api.stats.endpoints import leaguedashplayerstats, teamgamelog
from nba_api.stats.static import teams as nba_teams


# -------------------------
# Time helpers
# -------------------------
def us_eastern_today() -> dt.date:
    try:
        from zoneinfo import ZoneInfo
        now_et = dt.datetime.now(tz=ZoneInfo("America/New_York"))
        return now_et.date()
    except Exception:
        return (dt.datetime.utcnow() - dt.timedelta(hours=5)).date()


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

    if not all([host, dbname, user, password, port]):
        raise RuntimeError("DB env missing: set DATABASE_URL or SUPABASE_HOST/DB/USER/PASSWORD/PORT")

    return psycopg2.connect(
        host=host, dbname=dbname, user=user, password=password, port=int(port), sslmode="require"
    )


def ensure_schema():
    conn = db_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                CREATE TABLE IF NOT EXISTS public.nba_cache (
                  cache_key TEXT PRIMARY KEY
                );
                """)
                cur.execute("ALTER TABLE public.nba_cache ADD COLUMN IF NOT EXISTS payload_json TEXT;")
                cur.execute("ALTER TABLE public.nba_cache ADD COLUMN IF NOT EXISTS updated_at_tw TEXT;")
        print("[INFO] schema ensured: nba_cache", flush=True)
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


# -------------------------
# ESPN: figure out needed teams
# -------------------------
def fetch_espn_scoreboard(date_us: dt.date) -> List[dict]:
    ymd = date_us.strftime("%Y%m%d")
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    r = requests.get(url, params={"dates": ymd, "limit": 300}, timeout=25)
    r.raise_for_status()
    data = r.json()
    return data.get("events") or []


def teams_from_events(events: List[dict]) -> Set[str]:
    abbrs: Set[str] = set()
    for ev in events:
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        competitors = comp.get("competitors") or []
        for c in competitors:
            team = c.get("team") or {}
            ab = team.get("abbreviation")
            if ab:
                abbrs.add(ab)
    return abbrs


def build_needed_abbrs(anchor: dt.date, past_days: int, future_days: int) -> Set[str]:
    need: Set[str] = set()
    dates = [anchor - dt.timedelta(days=i) for i in range(past_days)] + \
            [anchor + dt.timedelta(days=i) for i in range(1, future_days + 1)]

    for d in dates:
        try:
            ev = fetch_espn_scoreboard(d)
            need |= teams_from_events(ev)
            print(f"[INFO] ESPN {d.isoformat()} teams={len(teams_from_events(ev))} total_need={len(need)}", flush=True)
        except Exception as e:
            print(f"[WARN] ESPN failed {d.isoformat()} err={e}", flush=True)
            continue

    return need


# -------------------------
# nba_api robust fetch
# -------------------------
def fetch_safe_df(endpoint_cls, retries: int = 4, base_sleep: float = 1.0, **kwargs) -> pd.DataFrame:
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
            ep = endpoint_cls(timeout=60, headers=headers, **kwargs)
            d = ep.get_dict()
            rs = d["resultSets"][0]
            return pd.DataFrame(rs["rowSet"], columns=rs["headers"])
        except Exception as e:
            if attempt < retries:
                sleep_s = base_sleep * (attempt + 1) * (attempt + 1)
                print(f"[WARN] retry {attempt+1}/{retries} {endpoint_cls.__name__} err={e} sleep={sleep_s:.1f}s", flush=True)
                time.sleep(sleep_s)
            else:
                print(f"[WARN] FAILED {endpoint_cls.__name__} err={e}", flush=True)
                return pd.DataFrame()


def main():
    ensure_schema()

    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()
    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()
    if override:
        anchor = dt.datetime.strptime(override, "%m/%d/%Y").date()
    else:
        anchor = us_eastern_today()

    past_days = int((os.environ.get("CACHE_PAST_DAYS") or "7").strip())
    future_days = int((os.environ.get("CACHE_FUTURE_DAYS") or "7").strip())

    print(f"[INFO] cache start season={season} anchor_us={anchor.isoformat()} past={past_days} future={future_days}", flush=True)

    # 1) figure out needed teams
    needed_abbrs = build_needed_abbrs(anchor, past_days=past_days, future_days=future_days)
    if not needed_abbrs:
        # fallback: cache nothing but still write meta
        cache_put("cache_meta", {"season": season, "updated_at_tw": now_tw_str(), "note": "no games found in window"})
        print("[OK] no teams needed; cache_meta written", flush=True)
        return

    print(f"[INFO] needed teams ({len(needed_abbrs)}): {sorted(list(needed_abbrs))}", flush=True)

    # 2) cache player_stats (useful for base_diff)
    ps = fetch_safe_df(
        leaguedashplayerstats.LeagueDashPlayerStats,
        season=season,
        per_mode_detailed="PerGame",
    )
    cache_put(f"player_stats:{season}", {"season": season, "rows": ps.to_dict(orient="records")})
    print(f"[OK] cached player_stats rows={len(ps)}", flush=True)

    # 3) cache only needed team logs
    all_teams = nba_teams.get_teams()
    abbr_to_id = {t["abbreviation"]: int(t["id"]) for t in all_teams}

    # intersect with actual NBA abbreviations
    needed = [ab for ab in sorted(list(needed_abbrs)) if ab in abbr_to_id]
    print(f"[INFO] caching team_logs count={len(needed)}", flush=True)

    for idx, abbr in enumerate(needed, start=1):
        tid = abbr_to_id[abbr]
        print(f"[INFO] ({idx}/{len(needed)}) fetching team_log {abbr}", flush=True)

        log = fetch_safe_df(teamgamelog.TeamGameLog, team_id=tid, season=season)

        cache_put(f"team_log:{season}:{abbr}", {"season": season, "abbr": abbr, "rows": log.to_dict(orient="records")})
        print(f"[OK] cached team_log {abbr} rows={len(log)}", flush=True)

        time.sleep(0.6)

    cache_put("cache_meta", {
        "season": season,
        "anchor_us": anchor.strftime("%Y-%m-%d"),
        "window": {"past_days": past_days, "future_days": future_days},
        "teams": needed,
        "updated_at_tw": now_tw_str(),
    })
    print("[OK] cache complete", flush=True)


if __name__ == "__main__":
    main()
