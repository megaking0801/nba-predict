import os, re, time, random
from datetime import datetime, timedelta
import pytz
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
import requests

from nba_api.stats.library.http import NBAStatsHTTP
from nba_api.stats.static import teams as static_teams

# -------------------------
# TZ
# -------------------------
tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

# -------------------------
# Teams
# -------------------------
ALL_TEAMS = static_teams.get_teams()
VALID_TEAM_IDS = set(t["id"] for t in ALL_TEAMS)
ID_MAP = {t["id"]: t["abbreviation"] for t in ALL_TEAMS}

TEAM_MAP = {
    "ATL": ["Atlanta Hawks", "老鷹"], "BKN": ["Brooklyn Nets", "籃網"], "BOS": ["Boston Celtics", "塞爾提克"],
    "CHA": ["Charlotte Hornets", "黃蜂"], "CHI": ["Chicago Bulls", "公牛"], "CLE": ["Cleveland Cavaliers", "騎士"],
    "DAL": ["Dallas Mavericks", "獨行俠"], "DEN": ["Denver Nuggets", "金塊"], "DET": ["Detroit Pistons", "活塞"],
    "GSW": ["Golden State Warriors", "勇士"], "HOU": ["Houston Rockets", "火箭"], "IND": ["Indiana Pacers", "溜馬"],
    "LAC": ["LA Clippers", "快艇"], "LAL": ["Los Angeles Lakers", "湖人"], "MEM": ["Memphis Grizzlies", "灰熊"],
    "MIA": ["Miami Heat", "熱火"], "MIL": ["Milwaukee Bucks", "公鹿"], "MIN": ["Minnesota Timberwolves", "灰狼"],
    "NOP": ["New Orleans Pelicans", "鵜鶘"], "NYK": ["New York Knicks", "尼克"], "OKC": ["Oklahoma City Thunder", "雷霆"],
    "ORL": ["Orlando Magic", "魔術"], "PHI": ["Philadelphia 76ers", "76人"], "PHX": ["Phoenix Suns", "太陽"],
    "POR": ["Portland Trail Blazers", "拓荒者"], "SAC": ["Sacramento Kings", "國王"], "SAS": ["San Antonio Spurs", "馬刺"],
    "TOR": ["Toronto Raptors", "暴龍"], "UTA": ["Utah Jazz", "爵士"], "WAS": ["Washington Wizards", "巫師"],
}
TEAM_NAME_CH = {k: v[1] for k, v in TEAM_MAP.items()}

ODDS_TEAMNAME_TO_ABBR = {
    "atlanta hawks": "ATL",
    "brooklyn nets": "BKN",
    "boston celtics": "BOS",
    "charlotte hornets": "CHA",
    "chicago bulls": "CHI",
    "cleveland cavaliers": "CLE",
    "dallas mavericks": "DAL",
    "denver nuggets": "DEN",
    "detroit pistons": "DET",
    "golden state warriors": "GSW",
    "houston rockets": "HOU",
    "indiana pacers": "IND",
    "la clippers": "LAC",
    "los angeles clippers": "LAC",
    "la lakers": "LAL",
    "los angeles lakers": "LAL",
    "memphis grizzlies": "MEM",
    "miami heat": "MIA",
    "milwaukee bucks": "MIL",
    "minnesota timberwolves": "MIN",
    "new orleans pelicans": "NOP",
    "new york knicks": "NYK",
    "oklahoma city thunder": "OKC",
    "orlando magic": "ORL",
    "philadelphia 76ers": "PHI",
    "phoenix suns": "PHX",
    "portland trail blazers": "POR",
    "sacramento kings": "SAC",
    "san antonio spurs": "SAS",
    "toronto raptors": "TOR",
    "utah jazz": "UTA",
    "washington wizards": "WAS",
}

SCOREBOARD_V3_URL = "https://stats.nba.com/stats/scoreboardv3"

# -------------------------
# DB
# -------------------------
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

def db_init():
    sql = """
    CREATE TABLE IF NOT EXISTS games (
        game_id TEXT PRIMARY KEY,
        game_date_us TEXT,
        game_date_tw TEXT,
        season TEXT,
        away_abbr TEXT,
        home_abbr TEXT,
        away_name TEXT,
        home_name TEXT,

        home_spread DOUBLE PRECISION,
        home_odds DOUBLE PRECISION,
        away_odds DOUBLE PRECISION,
        line_source TEXT,

        base_diff DOUBLE PRECISION,
        f_edge DOUBLE PRECISION,
        cover_prob DOUBLE PRECISION,
        implied_prob DOUBLE PRECISION,
        edge_value DOUBLE PRECISION,
        ev DOUBLE PRECISION,
        pick_team TEXT,

        status TEXT,
        away_score INTEGER,
        home_score INTEGER,
        cover INTEGER,
        settled_at_tw TEXT,

        created_at_tw TEXT,
        updated_at_tw TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_games_date_us ON games (game_date_us);
    CREATE INDEX IF NOT EXISTS idx_games_date_tw ON games (game_date_tw);
    """
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()

def bulk_upsert(rows: list[dict]) -> int:
    if not rows:
        return 0
    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

    all_cols = sorted(set().union(*[r.keys() for r in rows]))
    if "game_id" not in all_cols:
        raise RuntimeError("bulk_upsert requires game_id")

    if "created_at_tw" not in all_cols:
        all_cols.append("created_at_tw")
    if "updated_at_tw" not in all_cols:
        all_cols.append("updated_at_tw")

    values = []
    for r in rows:
        rr = dict(r)
        rr.setdefault("created_at_tw", now_tw)
        rr["updated_at_tw"] = now_tw
        values.append([rr.get(c, None) for c in all_cols])

    updates = ",".join([f"{c}=EXCLUDED.{c}" for c in all_cols if c != "game_id"])
    sql = f"""
    INSERT INTO games ({",".join(all_cols)})
    VALUES %s
    ON CONFLICT (game_id) DO UPDATE SET
      {updates};
    """

    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=200)
        conn.commit()
    finally:
        conn.close()
    return len(rows)

# -------------------------
# NBA fetch (robust)
# -------------------------
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

# -------------------------
# Odds API (Pinnacle spreads)
# -------------------------
def norm_name(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s

def get_pinnacle_odds_map() -> dict:
    api_key = (os.environ.get("ODDS_API_KEY") or "").strip()
    if not api_key:
        return {}

    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "spreads",
        "bookmakers": "pinnacle",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code != 200:
            print(f"[WARN] odds api status={r.status_code}")
            return {}
        data = r.json()
    except Exception as e:
        print(f"[WARN] odds api error: {e}")
        return {}

    out = {}
    for g in data:
        try:
            home_name = norm_name(g.get("home_team", ""))
            away_name = norm_name(g.get("away_team", ""))

            home_abbr = ODDS_TEAMNAME_TO_ABBR.get(home_name)
            away_abbr = ODDS_TEAMNAME_TO_ABBR.get(away_name)
            if not home_abbr or not away_abbr:
                continue

            bk = None
            for b in g.get("bookmakers", []):
                if norm_name(b.get("key", "")) == "pinnacle":
                    bk = b
                    break
                if "pinnacle" in norm_name(b.get("title", "")):
                    bk = b
                    break
            if not bk:
                continue

            spreads = None
            for m in bk.get("markets", []):
                if m.get("key") == "spreads":
                    spreads = m
                    break
            if not spreads:
                continue

            home_spread = None
            home_odds = None
            away_odds = None

            for o in spreads.get("outcomes", []):
                nm = norm_name(o.get("name", ""))
                pt = o.get("point", None)
                pr = o.get("price", None)
                if pt is None or pr is None:
                    continue
                if nm == home_name:
                    home_spread = float(pt)
                    home_odds = float(pr)
                elif nm == away_name:
                    away_odds = float(pr)

            if home_spread is None or home_odds is None or away_odds is None:
                continue

            out[(away_abbr, home_abbr)] = {
                "home_spread": float(home_spread),
                "home_odds": float(home_odds),
                "away_odds": float(away_odds),
                "line_source": "Pinnacle ✅",
            }
        except Exception:
            continue

    return out

# -------------------------
# Metrics (job baseline; UI will overwrite with richer model later)
# -------------------------
PROB_SCALE = 12.0
PROB_FLOOR = 0.12
PROB_CEIL  = 0.88

def calc_cover_prob(edge_points: float) -> float:
    import math
    x = abs(edge_points) / PROB_SCALE
    p = 1.0 / (1.0 + math.exp(-x))
    return max(PROB_FLOOR, min(PROB_CEIL, p))

def compute_metrics(base_diff: float, home_spread: float, home_odds: float, away_odds: float, home_name_cn: str, away_name_cn: str):
    f_edge = float(base_diff) + float(home_spread)
    cover_prob = calc_cover_prob(f_edge)

    pick_team = home_name_cn if f_edge > 0 else away_name_cn
    odds = float(home_odds) if f_edge > 0 else float(away_odds)

    implied_prob = 1.0 / odds if odds and odds > 0 else 1.0
    edge_value = cover_prob - implied_prob
    ev = (cover_prob * odds) - 1.0

    return {
        "base_diff": float(base_diff),
        "f_edge": float(f_edge),
        "cover_prob": float(cover_prob),
        "implied_prob": float(implied_prob),
        "edge_value": float(edge_value),
        "ev": float(ev),
        "pick_team": str(pick_team),
    }

# -------------------------
# Main sync
# -------------------------
def main():
    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()
    db_init()

    today_us = datetime.now(us_east_tz).date()
    targets = [
        today_us.strftime("%m/%d/%Y"),
        (today_us + timedelta(days=1)).strftime("%m/%d/%Y"),
    ]

    pin_map = get_pinnacle_odds_map()
    total = 0

    for d in targets:
        df = fetch_scoreboardv3_df(d, retries=5, timeout=25)
        if df.empty:
            print(f"[WARN] scoreboard empty for {d}")
            continue

        # game_date_tw：用「該場的美東日期」轉台灣時區的日期（通常 +1 天）
        date_us_dt = datetime.strptime(d, "%m/%d/%Y")
        date_tw_dt = us_east_tz.localize(date_us_dt).astimezone(tw_tz)
        game_date_tw = date_tw_dt.strftime("%Y-%m-%d")

        rows = []
        for _, r in df.iterrows():
            hid = int(r["HOME_TEAM_ID"])
            aid = int(r["VISITOR_TEAM_ID"])
            home_abbr = ID_MAP.get(hid)
            away_abbr = ID_MAP.get(aid)
            if not home_abbr or not away_abbr:
                continue

            home_cn = TEAM_NAME_CH.get(home_abbr, home_abbr)
            away_cn = TEAM_NAME_CH.get(away_abbr, away_abbr)

            game_id = f"{away_abbr}_{home_abbr}_{d.replace('/','')}"

            pin = pin_map.get((away_abbr, home_abbr))
            if pin:
                sp = float(pin["home_spread"])
                oh = float(pin["home_odds"])
                oa = float(pin["away_odds"])
                src = pin["line_source"]
            else:
                sp, oh, oa, src = 0.0, 1.90, 1.90, "Fallback ⚠️"

            # ✅ job 的 base_diff 先用 0.0（避免 job 太重太容易 timeout）
            m = compute_metrics(0.0, sp, oh, oa, home_cn, away_cn)

            rows.append({
                "game_id": game_id,
                "game_date_us": d,
                "game_date_tw": game_date_tw,
                "season": season,
                "away_abbr": away_abbr,
                "home_abbr": home_abbr,
                "away_name": away_cn,
                "home_name": home_cn,

                "home_spread": sp,
                "home_odds": oh,
                "away_odds": oa,
                "line_source": src,

                "base_diff": m["base_diff"],
                "f_edge": m["f_edge"],
                "cover_prob": m["cover_prob"],
                "implied_prob": m["implied_prob"],
                "edge_value": m["edge_value"],
                "ev": m["ev"],
                "pick_team": m["pick_team"],

                "status": "scheduled",
                "away_score": None,
                "home_score": None,
                "cover": None,
                "settled_at_tw": None,
            })

        n = bulk_upsert(rows)
        total += n
        print(f"[OK] sync {d} (TW={game_date_tw}) rows={n}")

    print(f"[OK] sync_total_rows={total}")

if __name__ == "__main__":
    main()
