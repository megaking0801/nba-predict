import os, time, random
from datetime import datetime, timedelta
import pytz
import pandas as pd
import psycopg2

from nba_api.stats.library.http import NBAStatsHTTP  # ✅ 直接用底層 HTTP
from nba_api.stats.static import teams as static_teams

tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

ALL_TEAMS = static_teams.get_teams()
VALID_TEAM_IDS = set(t["id"] for t in ALL_TEAMS)
ID_MAP = {t["id"]: t["abbreviation"] for t in ALL_TEAMS}

SCOREBOARD_V3_URL = "https://stats.nba.com/stats/scoreboardv3"

def pg_conn():
    host = (os.environ.get("SUPABASE_HOST") or "").strip()
    db   = (os.environ.get("SUPABASE_DB") or "postgres").strip()
    user = (os.environ.get("SUPABASE_USER") or "").strip()
    pw   = (os.environ.get("SUPABASE_PASSWORD") or "").strip()
    port_raw = (os.environ.get("SUPABASE_PORT") or "").strip()
    port = int(port_raw) if port_raw.isdigit() else 5432

    return psycopg2.connect(
        host=host,
        dbname=db,
        user=user,
        password=pw,
        port=port,
        sslmode="require",
        connect_timeout=10,
    )

def settle_cover(home_score: int, away_score: int, home_spread: float):
    if home_score is None or away_score is None or home_spread is None:
        return None
    adjusted = float(home_score) + float(home_spread)
    if adjusted > float(away_score):
        return 1
    if adjusted < float(away_score):
        return 0
    return 2

def fetch_scoreboardv3_df(game_date_us: str, retries: int = 4, timeout: int = 20) -> pd.DataFrame:
    """
    用底層 HTTP 自己控 timeout + retry + headers。
    避免 GitHub Actions 偶發 stats.nba.com 逾時就整個 job 掛掉。
    """
    # stats.nba.com 對 headers 很敏感
    headers = {
        "Host": "stats.nba.com",
        "Connection": "keep-alive",
        "Accept": "application/json, text/plain, */*",
        "x-nba-stats-token": "true",
        "x-nba-stats-origin": "stats",
        "Origin": "https://www.nba.com",
        "Referer": "https://www.nba.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    params = {
        "GameDate": game_date_us,
        "LeagueID": "00",
    }

    http = NBAStatsHTTP()
    last_err = None

    for i in range(retries + 1):
        try:
            # 小抖動，降低被擋機率
            if i > 0:
                time.sleep((2 ** (i - 1)) + random.random())

            resp = http.send_api_request(
                endpoint=SCOREBOARD_V3_URL,
                parameters=params,
                headers=headers,
                timeout=timeout,  # ✅ 控制讀取 timeout
            )
            data = resp.get_dict()

            rs0 = data["resultSets"][0]
            df = pd.DataFrame(rs0["rowSet"], columns=rs0["headers"])
            if df.empty or "HOME_TEAM_ID" not in df.columns:
                return pd.DataFrame()

            df = df[df["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)].copy()
            return df

        except Exception as e:
            last_err = e

    # 全部 retry 還是失敗：回空，不要讓 workflow 直接炸
    print(f"[WARN] scoreboardv3 fetch failed for {game_date_us}: {last_err}")
    return pd.DataFrame()

def main():
    today_us = datetime.now(us_east_tz).date()
    # ✅ 結算掃 US yesterday + US today（對台灣跨日最合理）
    dates = [
        (today_us - timedelta(days=1)).strftime("%m/%d/%Y"),
        today_us.strftime("%m/%d/%Y"),
    ]

    conn = pg_conn()
    updated = 0
    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

    try:
        with conn.cursor() as cur:
            for d in dates:
                df = fetch_scoreboardv3_df(d, retries=4, timeout=20)
                if df.empty:
                    print(f"[WARN] empty scoreboard for {d}")
                    continue

                for _, r in df.iterrows():
                    # GAME_STATUS_TEXT: "Final" / "Final/OT" / "Q3" etc
                    if "final" not in str(r.get("GAME_STATUS_TEXT", "")).lower():
                        continue

                    h_abbr = ID_MAP.get(int(r["HOME_TEAM_ID"]))
                    a_abbr = ID_MAP.get(int(r["VISITOR_TEAM_ID"]))
                    if not h_abbr or not a_abbr:
                        continue

                    game_id = f"{a_abbr}_{h_abbr}_{d.replace('/','')}"
                    home_score = int(r.get("HOME_TEAM_SCORE") or 0)
                    away_score = int(r.get("VISITOR_TEAM_SCORE") or 0)

                    cur.execute(
                        "SELECT home_spread FROM games WHERE game_id=%s",
                        (game_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        # 代表你 DB 裡沒有這場（可能 sync 沒跑到那天）
                        continue

                    home_spread = row[0]
                    cover = settle_cover(home_score, away_score, home_spread)
                    if cover is None:
                        continue

                    cur.execute(
                        """
                        UPDATE games SET
                          status='final',
                          home_score=%s,
                          away_score=%s,
                          cover=%s,
                          settled_at_tw=%s,
                          updated_at_tw=%s
                        WHERE game_id=%s
                        """,
                        (home_score, away_score, cover, now_tw, now_tw, game_id),
                    )
                    updated += 1

        conn.commit()
        print(f"[OK] settled updated_rows={updated} scan_dates={dates}")

    finally:
        conn.close()

if __name__ == "__main__":
    main()
