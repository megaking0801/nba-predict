# === settle_daily.py ===
import os
from datetime import datetime, timedelta
import pytz
import pandas as pd
from nba_api.stats.endpoints import scoreboardv3
from nba_api.stats.static import teams as static_teams
import psycopg2

tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

ALL_TEAMS = static_teams.get_teams()
ID_MAP = {t["id"]: t["abbreviation"] for t in ALL_TEAMS}
VALID_TEAM_IDS = set(t["id"] for t in ALL_TEAMS)

def pg_conn():
    return psycopg2.connect(
        host=os.environ["SUPABASE_HOST"],
        dbname=os.environ.get("SUPABASE_DB", "postgres"),
        user=os.environ["SUPABASE_USER"],
        password=os.environ["SUPABASE_PASSWORD"],
        port=int(os.environ.get("SUPABASE_PORT", "5432")),
        sslmode="require",
    )

def settle_cover(home_score, away_score, home_spread):
    adjusted = home_score + (home_spread or 0)
    if adjusted > away_score:
        return 1
    if adjusted < away_score:
        return 0
    return 2

def main():
    today_us = datetime.now(us_east_tz).date()
    dates = [
        (today_us - timedelta(days=1)).strftime("%m/%d/%Y"),
        today_us.strftime("%m/%d/%Y"),
    ]

    conn = pg_conn()
    updated = 0
    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

    with conn:
        with conn.cursor() as cur:
            for d in dates:
                sb = scoreboardv3.ScoreboardV3(game_date=d).get_dict()
                rs = sb["resultSets"][0]
                df = pd.DataFrame(rs["rowSet"], columns=rs["headers"])
                if df.empty:
                    continue

                df = df[df["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)]

                for _, r in df.iterrows():
                    if "Final" not in str(r["GAME_STATUS_TEXT"]):
                        continue

                    h_abbr = ID_MAP[int(r["HOME_TEAM_ID"])]
                    a_abbr = ID_MAP[int(r["VISITOR_TEAM_ID"])]
                    game_id = f"{a_abbr}_{h_abbr}_{d.replace('/','')}"

                    home_score = int(r["HOME_TEAM_SCORE"])
                    away_score = int(r["VISITOR_TEAM_SCORE"])

                    cur.execute("""
                        SELECT home_spread FROM games WHERE game_id=%s
                    """, (game_id,))
                    row = cur.fetchone()
                    if not row:
                        continue

                    home_spread = row[0]
                    cover = settle_cover(home_score, away_score, home_spread)

                    cur.execute("""
                        UPDATE games
                        SET status='final',
                            home_score=%s,
                            away_score=%s,
                            cover=%s,
                            settled_at_tw=%s
                        WHERE game_id=%s
                    """, (home_score, away_score, cover, now_tw, game_id))

                    updated += 1

    print(f"[OK] settled rows={updated}")

if __name__ == "__main__":
    main()
