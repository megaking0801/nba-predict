import os, time, random
from datetime import datetime, timedelta
import pytz
import pandas as pd
import psycopg2

from nba_api.stats.library.http import NBAStatsHTTP
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

    if not host or not user or not pw:
        raise RuntimeError("Missing DB env vars. Check GitHub Actions secrets.")

    return psycopg2.connect(
        host=host, dbname=db, user=user, password=pw, port=port,
        sslmode="require", connect_timeout=12
    )

def nba_headers():
    return {
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

def fetch_scoreboardv3_df(game_date_us: str, retries: int = 5, timeout: int = 25) -> pd.DataFrame:
    http = NBAStatsHTTP()
    params = {"GameDate": game_date_us, "LeagueID": "00"}
    last_err = None

    for i in range(retries + 1):
        try:
            if i > 0:
                time.sleep((2 ** (i - 1)) + random.random() * 0.7)

            resp = http.send_api_request(
                endpoint=SCOREBOARD_V3_URL,
                parameters=params,
                headers=nba_headers(),
                timeout=timeout,
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

    print(f"[WARN] scoreboard fetch failed for {game_date_us}: {last_err}")
    return pd.DataFrame()

def derive_us_token_from_row(row: pd.Series, fallback_us_mmddyyyy: str) -> str:
    date_est = row.get("GAME_DATE_EST", None)
    if date_est:
        try:
            dt_est_date = datetime.strptime(str(date_est), "%Y-%m-%d").date()
            game_date_us = dt_est_date.strftime("%m/%d/%Y")
        except Exception:
            game_date_us = fallback_us_mmddyyyy
    else:
        game_date_us = fallback_us_mmddyyyy

    return datetime.strptime(game_date_us, "%m/%d/%Y").strftime("%m%d%Y")

def settle_cover(home_score: int, away_score: int, home_spread: float):
    if home_score is None or away_score is None or home_spread is None:
        return None
    adjusted = float(home_score) + float(home_spread)
    if adjusted > float(away_score):
        return 1
    if adjusted < float(away_score):
        return 0
    return 2

def main():
    today_us = datetime.now(us_east_tz).date()
    targets = [
        (today_us - timedelta(days=1)).strftime("%m/%d/%Y"),
        today_us.strftime("%m/%d/%Y"),
    ]

    conn = pg_conn()
    updated = 0
    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

    try:
        with conn.cursor() as cur:
            for d in targets:
                df = fetch_scoreboardv3_df(d, retries=5, timeout=25)
                if df.empty:
                    print(f"[WARN] settle empty scoreboard for {d}")
                    continue

                for _, r in df.iterrows():
                    stxt = str(r.get("GAME_STATUS_TEXT", "")).lower()
                    if "final" not in stxt:
                        continue

                    hid = int(r["HOME_TEAM_ID"])
                    aid = int(r["VISITOR_TEAM_ID"])
                    home_abbr = ID_MAP.get(hid)
                    away_abbr = ID_MAP.get(aid)
                    if not home_abbr or not away_abbr:
                        continue

                    us_token = derive_us_token_from_row(r, fallback_us_mmddyyyy=d)
                    game_id = f"{away_abbr}_{home_abbr}_{us_token}"

                    home_score = int(r.get("HOME_TEAM_SCORE") or 0)
                    away_score = int(r.get("VISITOR_TEAM_SCORE") or 0)

                    cur.execute("select home_spread from public.games where game_id=%s", (game_id,))
                    row_db = cur.fetchone()
                    if not row_db:
                        continue
                    home_spread = row_db[0]

                    cover = settle_cover(home_score, away_score, home_spread)
                    if cover is None:
                        continue

                    cur.execute(
                        """
                        update public.games set
                          status='final',
                          home_score=%s,
                          away_score=%s,
                          cover=%s,
                          settled_at_tw=%s,
                          updated_at_tw=%s
                        where game_id=%s
                        """,
                        (home_score, away_score, cover, now_tw, now_tw, game_id),
                    )
                    updated += 1

        conn.commit()
        print(f"[OK] settle_targets={targets} updated_rows={updated}")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
