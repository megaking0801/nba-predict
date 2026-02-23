import os, time, random
from datetime import datetime, timedelta
import pytz
import pandas as pd
import psycopg2
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from nba_api.stats.static import teams as static_teams

tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

ALL_TEAMS = static_teams.get_teams()
VALID_TEAM_IDS = set(int(t["id"]) for t in ALL_TEAMS)
ID_MAP = {int(t["id"]): t["abbreviation"] for t in ALL_TEAMS}

SCOREBOARD_V3_URL = "https://stats.nba.com/stats/scoreboardv3"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

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

def stats_headers():
    return {
        "Host": "stats.nba.com",
        "Connection": "keep-alive",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
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

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def mmddyyyy_to_yyyymmdd(mmddyyyy: str) -> str:
    return datetime.strptime(mmddyyyy, "%m/%d/%Y").strftime("%Y%m%d")

# -------------------------
# Fetch stats finals
# -------------------------
def fetch_finals_stats(session: requests.Session, game_date_us: str, max_seconds: int = 35) -> list[dict]:
    start = time.time()
    attempt = 0
    params = {"GameDate": game_date_us, "LeagueID": "00"}

    while True:
        attempt += 1
        if time.time() - start > max_seconds:
            print(f"[WARN] stats giveup date={game_date_us} exceeded {max_seconds}s")
            return []

        time.sleep(0.2 + random.random() * 0.4)
        try:
            r = session.get(SCOREBOARD_V3_URL, params=params, headers=stats_headers(), timeout=(6, 18))
            if r.status_code != 200:
                print(f"[WARN] stats status={r.status_code} date={game_date_us} attempt={attempt}")
                if r.status_code == 429:
                    time.sleep(1.0 + random.random())
                continue

            data = r.json()
            rs0 = data["resultSets"][0]
            df = pd.DataFrame(rs0["rowSet"], columns=rs0["headers"])
            if df.empty:
                return []

            out = []
            for _, row in df.iterrows():
                try:
                    stxt = str(row.get("GAME_STATUS_TEXT", "")).lower()
                    if "final" not in stxt:
                        continue

                    hid = int(row.get("HOME_TEAM_ID"))
                    aid = int(row.get("VISITOR_TEAM_ID"))
                    if hid not in VALID_TEAM_IDS or aid not in VALID_TEAM_IDS:
                        continue

                    home_abbr = ID_MAP.get(hid)
                    away_abbr = ID_MAP.get(aid)
                    if not home_abbr or not away_abbr:
                        continue

                    date_est = row.get("GAME_DATE_EST", None)
                    if date_est:
                        try:
                            dt_est = datetime.strptime(str(date_est), "%Y-%m-%d").date()
                            game_date_us_eff = dt_est.strftime("%m/%d/%Y")
                        except Exception:
                            game_date_us_eff = game_date_us
                    else:
                        game_date_us_eff = game_date_us

                    out.append({
                        "source": "stats",
                        "game_date_us": game_date_us_eff,
                        "home_abbr": home_abbr,
                        "away_abbr": away_abbr,
                        "home_score": int(row.get("HOME_TEAM_SCORE") or 0),
                        "away_score": int(row.get("VISITOR_TEAM_SCORE") or 0),
                    })
                except Exception:
                    continue

            print(f"[OK] stats settle date={game_date_us} finals={len(out)} attempt={attempt}")
            return out

        except Exception as e:
            print(f"[WARN] stats error date={game_date_us} attempt={attempt}: {e}")
            continue

# -------------------------
# Fetch ESPN finals
# -------------------------
def fetch_finals_espn(session: requests.Session, game_date_us: str, max_seconds: int = 18) -> list[dict]:
    yyyymmdd = mmddyyyy_to_yyyymmdd(game_date_us)

    start = time.time()
    attempt = 0
    while True:
        attempt += 1
        if time.time() - start > max_seconds:
            print(f"[WARN] espn giveup date={game_date_us} exceeded {max_seconds}s")
            return []

        time.sleep(0.15 + random.random() * 0.25)
        try:
            r = session.get(ESPN_SCOREBOARD_URL, params={"dates": yyyymmdd}, timeout=(6, 18))
            if r.status_code != 200:
                print(f"[WARN] espn status={r.status_code} date={game_date_us} attempt={attempt}")
                if r.status_code in (401, 403, 404):
                    return []
                continue

            data = r.json()
            events = data.get("events", []) or []
            out = []

            for ev in events:
                try:
                    comps = ev.get("competitions", []) or []
                    if not comps:
                        continue
                    c0 = comps[0]

                    st = ((c0.get("status", {}) or {}).get("type", {}) or {}).get("name", "")
                    if "final" not in str(st).lower():
                        continue

                    competitors = c0.get("competitors", []) or []
                    if len(competitors) != 2:
                        continue

                    home = next((x for x in competitors if x.get("homeAway") == "home"), None)
                    away = next((x for x in competitors if x.get("homeAway") == "away"), None)
                    if not home or not away:
                        continue

                    home_abbr = (home.get("team", {}) or {}).get("abbreviation")
                    away_abbr = (away.get("team", {}) or {}).get("abbreviation")
                    if not home_abbr or not away_abbr:
                        continue

                    hs = int(float(home.get("score") or 0))
                    a_s = int(float(away.get("score") or 0))

                    out.append({
                        "source": "espn",
                        "game_date_us": game_date_us,
                        "home_abbr": home_abbr,
                        "away_abbr": away_abbr,
                        "home_score": hs,
                        "away_score": a_s,
                    })
                except Exception:
                    continue

            print(f"[OK] espn settle date={game_date_us} finals={len(out)} attempt={attempt}")
            return out

        except Exception as e:
            print(f"[WARN] espn error date={game_date_us} attempt={attempt}: {e}")
            continue

def fetch_finals(session: requests.Session, game_date_us: str) -> list[dict]:
    g = fetch_finals_stats(session, game_date_us)
    if g:
        return g
    return fetch_finals_espn(session, game_date_us)

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
    override = (os.environ.get("OVERRIDE_US_DATE") or "").strip()
    if override:
        targets = [override]
    else:
        today_us = datetime.now(us_east_tz).date()
        targets = [
            (today_us - timedelta(days=1)).strftime("%m/%d/%Y"),
            today_us.strftime("%m/%d/%Y"),
        ]

    session = make_session()
    conn = pg_conn()
    updated = 0
    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

    try:
        with conn.cursor() as cur:
            for d in targets:
                finals = fetch_finals(session, d)
                if not finals:
                    print(f"[WARN] no finals for {d} (stats failed and espn none final)")
                    continue

                for g in finals:
                    game_date_us = g["game_date_us"]
                    us_token = datetime.strptime(game_date_us, "%m/%d/%Y").strftime("%m%d%Y")
                    game_id = f"{g['away_abbr']}_{g['home_abbr']}_{us_token}"

                    home_score = int(g["home_score"])
                    away_score = int(g["away_score"])

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
