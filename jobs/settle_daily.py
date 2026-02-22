# jobs/settle_daily.py
import os, time
from datetime import datetime

import pytz
import pandas as pd
import psycopg2
from nba_api.stats.endpoints import scoreboardv2
from nba_api.stats.static import teams as static_teams

tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

def pg_conn():
    host = os.environ["SUPABASE_HOST"]
    db   = os.environ.get("SUPABASE_DB", "postgres")
    user = os.environ["SUPABASE_USER"]
    pw   = os.environ["SUPABASE_PASSWORD"]
    port = int(os.environ.get("SUPABASE_PORT", "5432"))
    return psycopg2.connect(
        host=host, dbname=db, user=user, password=pw, port=port,
        connect_timeout=10, sslmode="require"
    )

def fetch_safe_df(endpoint, retries: int = 2, sleep_s: float = 0.8, **kwargs) -> pd.DataFrame:
    for attempt in range(retries + 1):
        try:
            r = endpoint(**kwargs).get_dict()
            res = r["resultSets"][0]
            return pd.DataFrame(res["rowSet"], columns=res["headers"])
        except Exception:
            if attempt < retries:
                time.sleep(sleep_s * (attempt + 1))
            else:
                return pd.DataFrame()

def get_scoreboard_status_map(game_date_us: str) -> dict:
    ALL_TEAMS = static_teams.get_teams()
    ID_MAP = {t["id"]: t["abbreviation"] for t in ALL_TEAMS}

    sbx = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=game_date_us)
    out = {}
    if sbx.empty:
        return out

    for _, r in sbx.iterrows():
        try:
            hid = int(r.get("HOME_TEAM_ID"))
            aid = int(r.get("VISITOR_TEAM_ID"))
            home_abbr = ID_MAP.get(hid)
            away_abbr = ID_MAP.get(aid)
            if not home_abbr or not away_abbr:
                continue

            hs = r.get("HOME_TEAM_SCORE", None)
            as_ = r.get("VISITOR_TEAM_SCORE", None)
            stxt = str(r.get("GAME_STATUS_TEXT", "")).lower()

            if "final" in stxt:
                status = "final"
            elif ("q" in stxt) or ("half" in stxt) or ("end" in stxt) or ("ot" in stxt):
                status = "in_progress"
            else:
                status = "scheduled"

            out[(away_abbr, home_abbr)] = {
                "status": status,
                "away_score": int(as_) if as_ is not None and str(as_).isdigit() else None,
                "home_score": int(hs) if hs is not None and str(hs).isdigit() else None,
            }
        except Exception:
            continue
    return out

def settle_cover(home_score: int, away_score: int, home_spread: float):
    if home_score is None or away_score is None or home_spread is None:
        return None
    adjusted = float(home_score) + float(home_spread)
    if adjusted > float(away_score):
        return 1
    if adjusted < float(away_score):
        return 0
    return 2

def update_results_and_settle(game_date_us: str) -> int:
    status_map = get_scoreboard_status_map(game_date_us)
    if not status_map:
        return 0

    conn = pg_conn()
    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")
    updated = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT game_id, away_abbr, home_abbr, home_spread FROM games WHERE game_date_us=%s",
                (game_date_us,),
            )
            rows = cur.fetchall()

            for game_id, away_abbr, home_abbr, home_spread in rows:
                key = (away_abbr, home_abbr)
                if key not in status_map:
                    continue

                s = status_map[key]
                status = s["status"]
                away_score = s["away_score"]
                home_score = s["home_score"]

                cover = None
                settled_at = None
                if status == "final" and home_score is not None and away_score is not None:
                    cover = settle_cover(home_score, away_score, home_spread)
                    settled_at = now_tw

                cur.execute(
                    """
                    UPDATE games SET
                      status=%s,
                      away_score=%s,
                      home_score=%s,
                      cover=COALESCE(%s, cover),
                      settled_at_tw=COALESCE(%s, settled_at_tw),
                      updated_at_tw=%s
                    WHERE game_id=%s
                    """,
                    (status, away_score, home_score, cover, settled_at, now_tw, game_id),
                )
                updated += 1

        conn.commit()
        return updated
    finally:
        conn.close()

def main():
    # 直接結算「DB 裡存在的非 final 日期」（<= 今天美東）— 最保險
    today_us = datetime.now(us_east_tz).date()

    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT game_date_us
                FROM games
                WHERE (status IS NULL OR status <> 'final')
            """)
            dates = [r[0] for r in cur.fetchall() if r and r[0]]
    finally:
        conn.close()

    def parse_us(d):
        return datetime.strptime(d, "%m/%d/%Y").date()

    dates = sorted(set(dates), key=parse_us)
    dates = [d for d in dates if parse_us(d) <= today_us]

    total = 0
    for d in dates:
        total += update_results_and_settle(d)

    print(f"[OK] settle_scan_dates={len(dates)} updated_rows={total}")

if __name__ == "__main__":
    main()
