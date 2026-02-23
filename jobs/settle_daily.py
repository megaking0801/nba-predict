#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import datetime as dt
from typing import Optional, List, Dict, Tuple

import requests
import psycopg2

# -----------------------------
# time utils
# -----------------------------
def us_eastern_today() -> dt.date:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(tz=ZoneInfo("America/New_York")).date()
    except Exception:
        return (dt.datetime.utcnow() - dt.timedelta(hours=5)).date()

def now_tw_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(tz=ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

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
    port = int((os.environ.get("SUPABASE_PORT") or "5432").strip())

    if not all([host, dbname, user, password]):
        raise RuntimeError("DB env missing")

    return psycopg2.connect(
        host=host, dbname=dbname, user=user, password=password, port=port,
        sslmode="require"
    )

# -----------------------------
# ESPN
# -----------------------------
def fetch_espn_scoreboard(date_us: dt.date) -> List[dict]:
    ymd = date_us.strftime("%Y%m%d")
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    r = requests.get(url, params={"dates": ymd, "limit": 300}, timeout=25)
    r.raise_for_status()
    data = r.json()
    return data.get("events") or []

def parse_final_games(events: List[dict], date_us: dt.date) -> List[dict]:
    """
    Return list of finals:
      { away_abbr, home_abbr, away_score, home_score, status='final' }
    """
    out = []
    for ev in events:
        try:
            comp = (ev.get("competitions") or [])[0]
            competitors = comp.get("competitors") or []
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue

            st = (comp.get("status") or {}).get("type") or {}
            state = (st.get("state") or "").lower()
            completed = bool(st.get("completed"))

            is_final = completed or state == "post"
            if not is_final:
                continue

            home_abbr = (home.get("team") or {}).get("abbreviation")
            away_abbr = (away.get("team") or {}).get("abbreviation")
            if not home_abbr or not away_abbr:
                continue

            hs = home.get("score")
            a_s = away.get("score")
            if hs is None or a_s is None:
                continue

            out.append({
                "home_abbr": home_abbr,
                "away_abbr": away_abbr,
                "home_score": int(hs),
                "away_score": int(a_s),
                "ymd": date_us.strftime("%Y%m%d"),
            })
        except Exception:
            continue
    return out

# -----------------------------
# settle logic
# -----------------------------
def settle_cover(home_score: int, away_score: int, home_spread: float) -> int:
    """
    home_spread: 主讓分為負、主受讓為正（你目前系統定義）
    adjusted = home_score + home_spread
    """
    adjusted = float(home_score) + float(home_spread)
    if adjusted > float(away_score):
        return 1
    if adjusted < float(away_score):
        return 0
    return 2

UPDATE_SQL = """
UPDATE public.games
SET
  status='final',
  home_score=%s,
  away_score=%s,
  cover=%s,
  settled_at_tw=%s,
  updated_at_tw=%s
WHERE game_id=%s
  AND home_spread IS NOT NULL
;
"""

UPDATE_SCORE_ONLY_SQL = """
UPDATE public.games
SET
  status='final',
  home_score=%s,
  away_score=%s,
  updated_at_tw=%s
WHERE game_id=%s
;
"""

def main():
    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()
    if override:
        anchor = dt.datetime.strptime(override, "%m/%d/%Y").date()
    else:
        anchor = us_eastern_today()

    past_days = int((os.environ.get("SETTLE_PAST_DAYS") or "120").strip())
    past_days = max(1, past_days)

    dates = [anchor - dt.timedelta(days=i) for i in range(past_days)]
    now_tw = now_tw_str()

    conn = db_connect()
    updated_cover = 0
    updated_score_only = 0
    missing_rows = 0

    try:
        with conn:
            with conn.cursor() as cur:
                for d in dates:
                    events = fetch_espn_scoreboard(d)
                    finals = parse_final_games(events, d)
                    if not finals:
                        continue

                    for g in finals:
                        game_id = f"{g['ymd']}_{g['away_abbr']}_{g['home_abbr']}"

                        # 先拿 spread（沒有 spread 就只能更新比分與 final status）
                        cur.execute(
                            "SELECT home_spread FROM public.games WHERE game_id=%s",
                            (game_id,)
                        )
                        row = cur.fetchone()
                        if not row:
                            missing_rows += 1
                            continue

                        home_spread = row[0]
                        if home_spread is None:
                            cur.execute(
                                UPDATE_SCORE_ONLY_SQL,
                                (g["home_score"], g["away_score"], now_tw, game_id)
                            )
                            updated_score_only += 1
                            continue

                        cover = settle_cover(g["home_score"], g["away_score"], float(home_spread))
                        cur.execute(
                            UPDATE_SQL,
                            (g["home_score"], g["away_score"], cover, now_tw, now_tw, game_id)
                        )
                        updated_cover += 1

        print(f"[OK] settle done updated_cover={updated_cover} updated_score_only={updated_score_only} missing_rows={missing_rows}")

    finally:
        conn.close()

if __name__ == "__main__":
    main()
