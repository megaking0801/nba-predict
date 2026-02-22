# === sync_daily.py ===
import os, re, time, math, unicodedata, warnings
from datetime import datetime, timedelta
import pytz
import pandas as pd
import requests

from nba_api.stats.endpoints import scoreboardv3
from nba_api.stats.static import teams as static_teams
import psycopg2
from psycopg2.extras import execute_values

warnings.filterwarnings("ignore")

tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

ALL_TEAMS = static_teams.get_teams()
VALID_TEAM_IDS = set(t["id"] for t in ALL_TEAMS)
ID_MAP = {t["id"]: t["abbreviation"] for t in ALL_TEAMS}

def fetch_safe_df(endpoint_cls, retries=2, sleep_s=1.0, **kwargs):
    for i in range(retries + 1):
        try:
            r = endpoint_cls(**kwargs).get_dict()
            rs = r.get("resultSets") or r.get("resultSet")
            if isinstance(rs, list):
                headers = rs[0]["headers"]
                rows = rs[0]["rowSet"]
            else:
                headers = rs["headers"]
                rows = rs["rowSet"]
            return pd.DataFrame(rows, columns=headers)
        except Exception:
            if i < retries:
                time.sleep(sleep_s)
            else:
                return pd.DataFrame()

def pg_conn():
    host = os.environ["SUPABASE_HOST"]
    db   = os.environ.get("SUPABASE_DB", "postgres")
    user = os.environ["SUPABASE_USER"]
    pw   = os.environ["SUPABASE_PASSWORD"]
    port = int(os.environ.get("SUPABASE_PORT", "5432"))
    return psycopg2.connect(
        host=host,
        dbname=db,
        user=user,
        password=pw,
        port=port,
        sslmode="require",
    )

def bulk_upsert(rows):
    if not rows:
        return
    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

    cols = sorted(set().union(*[r.keys() for r in rows]))
    if "created_at_tw" not in cols:
        cols.append("created_at_tw")
    if "updated_at_tw" not in cols:
        cols.append("updated_at_tw")

    values = []
    for r in rows:
        r.setdefault("created_at_tw", now_tw)
        r["updated_at_tw"] = now_tw
        values.append([r.get(c) for c in cols])

    update_cols = ",".join([f"{c}=EXCLUDED.{c}" for c in cols if c != "game_id"])
    sql = f"""
    INSERT INTO games ({",".join(cols)})
    VALUES %s
    ON CONFLICT (game_id) DO UPDATE SET
      {update_cols};
    """

    conn = pg_conn()
    with conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=200)

def main():
    season = os.environ.get("NBA_SEASON", "2025-26")

    today_us = datetime.now(us_east_tz).date()
    dates = [
        today_us.strftime("%m/%d/%Y"),
        (today_us + timedelta(days=1)).strftime("%m/%d/%Y"),
    ]

    total = 0

    for game_date_us in dates:
        sb = fetch_safe_df(scoreboardv3.ScoreboardV3, game_date=game_date_us)
        if sb.empty or "HOME_TEAM_ID" not in sb.columns:
            print(f"[WARN] scoreboard empty for {game_date_us}")
            continue

        sb = sb[sb["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)].copy()

        # 👉 轉台灣日期
        date_us_dt = datetime.strptime(game_date_us, "%m/%d/%Y")
        date_tw_dt = us_east_tz.localize(date_us_dt).astimezone(tw_tz)
        game_date_tw = date_tw_dt.strftime("%Y-%m-%d")

        rows = []

        for _, r in sb.iterrows():
            h_id = int(r["HOME_TEAM_ID"])
            a_id = int(r["VISITOR_TEAM_ID"])
            h_abbr = ID_MAP[h_id]
            a_abbr = ID_MAP[a_id]

            game_id = f"{a_abbr}_{h_abbr}_{game_date_us.replace('/','')}"

            rows.append({
                "game_id": game_id,
                "game_date_us": game_date_us,
                "game_date_tw": game_date_tw,
                "season": season,
                "away_abbr": a_abbr,
                "home_abbr": h_abbr,
                "status": "scheduled",
                "cover": None,
            })

        bulk_upsert(rows)
        print(f"[OK] synced {len(rows)} games for {game_date_us} (TW={game_date_tw})")
        total += len(rows)

    print(f"[DONE] total={total}")

if __name__ == "__main__":
    main()
