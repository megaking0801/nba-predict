#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
jobs/settle_daily.py

- Reads recent games from DB
- Pulls ESPN scoreboard again to confirm final score
- Computes cover using home_spread and final score
- Writes:
    status=final, home_score, away_score, cover, settled_at_tw, margin
"""

import os
import datetime as dt
from typing import Dict, List, Optional, Tuple

import requests
import psycopg2
import psycopg2.extras


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
        raise RuntimeError("DB env missing: set DATABASE_URL or SUPABASE_*")

    return psycopg2.connect(host=host, dbname=dbname, user=user, password=password, port=int(port), sslmode="require")


def fetch_espn_scoreboard(date_us: dt.date) -> List[dict]:
    ymd = date_us.strftime("%Y%m%d")
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    r = requests.get(url, params={"dates": ymd, "limit": 300}, timeout=25)
    r.raise_for_status()
    return (r.json().get("events") or [])


def parse_final_scores(events: List[dict]) -> Dict[Tuple[str, str], Tuple[int, int, str]]:
    """
    Return map (away_abbr, home_abbr) -> (away_score, home_score, status)
    status in {scheduled, in_progress, final}
    """
    out = {}
    for ev in events:
        try:
            comps = ev.get("competitions") or []
            if not comps:
                continue
            comp = comps[0]
            competitors = comp.get("competitors") or []
            if len(competitors) < 2:
                continue

            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue

            home_team = home.get("team") or {}
            away_team = away.get("team") or {}

            home_abbr = home_team.get("abbreviation")
            away_abbr = away_team.get("abbreviation")
            if not home_abbr or not away_abbr:
                continue

            st = (comp.get("status") or {}).get("type") or {}
            state = (st.get("state") or "").lower()
            completed = bool(st.get("completed"))

            if completed or state == "post":
                status = "final"
            elif state == "in":
                status = "in_progress"
            else:
                status = "scheduled"

            away_score = int(away.get("score")) if away.get("score") is not None else None
            home_score = int(home.get("score")) if home.get("score") is not None else None

            if away_score is None or home_score is None:
                continue

            out[(away_abbr, home_abbr)] = (away_score, home_score, status)
        except Exception:
            continue
    return out


UPDATE_SQL = """
UPDATE public.games
SET
  status = 'final',
  away_score = %(away_score)s,
  home_score = %(home_score)s,
  cover = %(cover)s,
  margin = %(margin)s,
  settled_at_tw = %(settled_at_tw)s,
  updated_at_tw = %(settled_at_tw)s
WHERE game_id = %(game_id)s;
"""


def compute_cover(home_score: int, away_score: int, home_spread: float) -> int:
    """
    cover:
      1 = home covers
      0 = not
      2 = push
    """
    margin = home_score - away_score
    adj = margin + home_spread
    if abs(adj) < 1e-9:
        return 2
    return 1 if adj > 0 else 0


def main():
    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()
    if override:
        anchor_us = dt.datetime.strptime(override, "%m/%d/%Y").date()
    else:
        anchor_us = us_eastern_today()

    # settle last N days to be safe
    settle_days = int((os.environ.get("SETTLE_PAST_DAYS") or "14").strip())
    dates = [anchor_us - dt.timedelta(days=i) for i in range(settle_days)]

    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT game_id, game_date_us, away_abbr, home_abbr, home_spread, status
                FROM public.games
                WHERE to_date(game_date_us,'MM/DD/YYYY') >= (current_date - interval %s)
            """, (f"{settle_days} days",))
            rows = cur.fetchall()
    finally:
        conn.close()

    # group by date
    by_date: Dict[str, List[tuple]] = {}
    for game_id, game_date_us, away_abbr, home_abbr, home_spread, status in rows:
        by_date.setdefault(game_date_us, []).append((game_id, away_abbr, home_abbr, home_spread, status))

    total_settled = 0
    for d in dates:
        date_us_str = d.strftime("%m/%d/%Y")
        games = by_date.get(date_us_str, [])
        if not games:
            continue

        try:
            events = fetch_espn_scoreboard(d)
            final_map = parse_final_scores(events)
        except Exception as e:
            print(f"[WARN] espn fetch failed date={date_us_str} err={e}")
            continue

        updates = []
        for game_id, away_abbr, home_abbr, home_spread, status in games:
            key = (away_abbr, home_abbr)
            if key not in final_map:
                continue
            away_score, home_score, espn_status = final_map[key]
            if espn_status != "final":
                continue
            if home_spread is None:
                # cannot compute cover without spread
                continue

            cover = compute_cover(home_score, away_score, float(home_spread))
            margin = float(home_score - away_score)

            updates.append({
                "game_id": game_id,
                "away_score": away_score,
                "home_score": home_score,
                "cover": cover,
                "margin": margin,
                "settled_at_tw": now_tw_str(),
            })

        if updates:
            conn = db_connect()
            try:
                with conn:
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_batch(cur, UPDATE_SQL, updates, page_size=200)
                total_settled += len(updates)
                print(f"[INFO] settled date={date_us_str} n={len(updates)}")
            finally:
                conn.close()

    print(f"[OK] settle complete total={total_settled}")


if __name__ == "__main__":
    main()
