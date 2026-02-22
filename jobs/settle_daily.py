import os, time, warnings
from datetime import datetime, timedelta
import pytz
import pandas as pd

from nba_api.stats.endpoints import scoreboardv3
from nba_api.stats.static import teams as static_teams

import psycopg2

warnings.filterwarnings("ignore")

tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

ALL_TEAMS = static_teams.get_teams()
VALID_TEAM_IDS = set(t["id"] for t in ALL_TEAMS)
ID_MAP = {t["id"]: t["abbreviation"] for t in ALL_TEAMS}

def fetch_safe_df(endpoint_cls, retries: int = 2, sleep_s: float = 0.8, **kwargs) -> pd.DataFrame:
    for attempt in range(retries + 1):
        try:
            r = endpoint_cls(**kwargs).get_dict()
            rs = r.get("resultSets") or r.get("resultSet") or []
            if isinstance(rs, dict):
                headers = rs.get("headers", [])
                rows = rs.get("rowSet", [])
                return pd.DataFrame(rows, columns=headers)
            if isinstance(rs, list) and len(rs) > 0:
                res0 = rs[0]
                headers = res0.get("headers", [])
                rows = res0.get("rowSet", [])
                return pd.DataFrame(rows, columns=headers)
            return pd.DataFrame()
        except Exception:
            if attempt < retries:
                time.sleep(sleep_s * (attempt + 1))
            else:
                return pd.DataFrame()

def pg_conn():
    host = (os.environ.get("SUPABASE_HOST") or "").strip()
    if not host:
        raise RuntimeError("SUPABASE_HOST is empty. Check GitHub Actions secrets.")
    db   = (os.environ.get("SUPABASE_DB") or "postgres").strip()
    user = (os.environ.get("SUPABASE_USER") or "").strip()
    pw   = (os.environ.get("SUPABASE_PASSWORD") or "").strip()
    if not user or not pw:
        raise RuntimeError("SUPABASE_USER or SUPABASE_PASSWORD is empty. Check secrets.")

    port_raw = (os.environ.get("SUPABASE_PORT") or "").strip()
    port = int(port_raw) if port_raw.isdigit() else 5432

    return psycopg2.connect(
        host=host,
        dbname=db,
        user=user,
        password=pw,
        port=port,
        connect_timeout=10,
        sslmode="require",
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

def get_scoreboard_status_map(game_date_us: str) -> dict:
    sbx = fetch_safe_df(scoreboardv3.ScoreboardV3, game_date=game_date_us, retries=2, sleep_s=0.9)
    out = {}
    if sbx.empty or "HOME_TEAM_ID" not in sbx.columns:
        return out

    sbx = sbx[sbx["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)].copy()

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

            def to_int(x):
                try:
                    return int(x)
                except Exception:
                    return None

            out[(away_abbr, home_abbr)] = {
                "status": status,
                "away_score": to_int(as_),
                "home_score": to_int(hs),
            }
        except Exception:
            continue
    return out

def update_results_and_settle_for_dates(date_list_us: list[str]) -> int:
    """
    對指定的美東日期清單：讀 DB 的 games，若 scoreboard 顯示 final，就更新比分與 cover
    """
    conn = pg_conn()
    updated = 0
    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

    try:
        with conn.cursor() as cur:
            for game_date_us in date_list_us:
                status_map = get_scoreboard_status_map(game_date_us)
                if not status_map:
                    continue

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
    today_us = datetime.now(us_east_tz).date()
    dates = [
        (today_us - timedelta(days=1)).strftime("%m/%d/%Y"),  # US yesterday
        today_us.strftime("%m/%d/%Y"),                        # US today
    ]

    n = update_results_and_settle_for_dates(dates)
    print(f"[OK] settle_scan_dates={dates}, updated_rows={n}")

if __name__ == "__main__":
    main()
