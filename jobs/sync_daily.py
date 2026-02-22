# jobs/sync_daily.py
import os, re, time, math, unicodedata
from datetime import datetime, timedelta

import pytz
import pandas as pd
import requests
import psycopg2
from psycopg2.extras import execute_values

from nba_api.stats.endpoints import scoreboardv2, leaguedashplayerstats, teamgamelog
from nba_api.stats.static import teams as static_teams

tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

# -----------------------------
# Team maps (same as your app)
# -----------------------------
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

# -----------------------------
# Helpers
# -----------------------------
def norm_name(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    s = re.sub(r"[^a-z\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

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

# -----------------------------
# Probability mapping
# -----------------------------
PROB_SCALE = 12.0
PROB_FLOOR = 0.12
PROB_CEIL  = 0.88

def calc_cover_prob(edge_points_signed: float) -> float:
    x = abs(edge_points_signed) / PROB_SCALE
    p = 1.0 / (1.0 + math.exp(-x))
    return min(max(p, PROB_FLOOR), PROB_CEIL)

# -----------------------------
# DB
# -----------------------------
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

def db_init():
    sql = """
    CREATE TABLE IF NOT EXISTS games (
        game_id TEXT PRIMARY KEY,
        game_date_us TEXT,
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

    CREATE INDEX IF NOT EXISTS idx_games_date ON games (game_date_us);
    """
    conn = pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()

def bulk_upsert(rows: list[dict]):
    if not rows:
        return
    now_tw = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

    all_cols = sorted(set().union(*[r.keys() for r in rows]))
    if "game_id" not in all_cols:
        return
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

# -----------------------------
# Odds API (Pinnacle)
# -----------------------------
def get_pinnacle_odds_for_date(game_date_us: str) -> dict:
    api_key = os.environ.get("ODDS_API_KEY", "")
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
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return {}
        data = r.json()
    except Exception:
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

            books = g.get("bookmakers", [])
            if not books:
                continue
            bk = books[0]
            mkts = bk.get("markets", [])
            spreads = next((m for m in mkts if m.get("key") == "spreads"), None)
            if not spreads:
                continue

            outcomes = spreads.get("outcomes", [])
            if len(outcomes) < 2:
                continue

            home_spread = home_odds = away_odds = None
            for o in outcomes:
                name = norm_name(o.get("name", ""))
                point = o.get("point", None)
                price = o.get("price", None)
                if point is None or price is None:
                    continue
                if name == home_name:
                    home_spread = float(point)
                    home_odds = float(price)
                elif name == away_name:
                    away_odds = float(price)

            if home_spread is None or home_odds is None or away_odds is None:
                continue

            out[(away_abbr, home_abbr)] = {
                "home_spread": float(home_spread),
                "home_odds": float(home_odds),
                "away_odds": float(away_odds),
                "ok": True,
            }
        except Exception:
            continue

    return out

# -----------------------------
# NBA Data (stats + context)
# -----------------------------
def get_player_stats(season: str) -> pd.DataFrame:
    ps = fetch_safe_df(
        leaguedashplayerstats.LeagueDashPlayerStats,
        season=season,
        per_mode_detailed="PerGame",
    )
    if ps.empty or "TEAM_ID" not in ps.columns or "PLAYER_NAME" not in ps.columns:
        return pd.DataFrame(columns=["PLAYER_NAME", "TEAM_ID", "PTS", "IMPACT", "NORM", "GP", "MIN"])

    for c in ["GP", "MIN", "PTS", "REB", "AST", "STL", "BLK", "TOV"]:
        if c not in ps.columns:
            ps[c] = 0

    ps = ps[(ps["GP"] >= 5) & (ps["MIN"] >= 10)].copy()

    ps["IMPACT"] = (
        ps["PTS"]
        + ps["REB"] * 1.1
        + ps["AST"] * 1.5
        + (ps["STL"] + ps["BLK"]) * 2
        - ps["TOV"] * 2
    )
    ps["NORM"] = ps["PLAYER_NAME"].astype(str).map(norm_name)
    return ps

def get_team_context(team_ids: list[int], game_date_us: str, season: str) -> dict:
    ctx = {}
    game_day = datetime.strptime(game_date_us, "%m/%d/%Y").date()
    prev_day = game_day - timedelta(days=1)

    for tid in team_ids:
        log = fetch_safe_df(teamgamelog.TeamGameLog, team_id=tid, season=season)
        is_b2b, recent_w = False, 0.5

        if not log.empty and "GAME_DATE" in log.columns and "WL" in log.columns:
            log = log.head(15).copy()
            log["GAME_DATE"] = pd.to_datetime(log["GAME_DATE"], errors="coerce").dt.date
            log = log.dropna(subset=["GAME_DATE"])

            prior = log[log["GAME_DATE"] < game_day].sort_values("GAME_DATE", ascending=False)
            if not prior.empty:
                last_game_date = prior.iloc[0]["GAME_DATE"]
                is_b2b = (last_game_date == prev_day)
                last5 = prior.head(5)
                if len(last5) > 0:
                    recent_w = (last5["WL"] == "W").mean()

        ctx[tid] = {"b2b": bool(is_b2b), "recent_w": float(recent_w)}
    return ctx

def compute_metrics(base_diff: float, home_spread: float, home_odds: float, away_odds: float, home_name: str, away_name: str):
    f_edge = base_diff + home_spread
    cover_prob = calc_cover_prob(f_edge)
    pick_team = home_name if f_edge > 0 else away_name
    odds = home_odds if f_edge > 0 else away_odds

    implied_prob = 1.0 / odds if odds and odds > 0 else 1.0
    edge_value = cover_prob - implied_prob
    ev = (cover_prob * odds) - 1.0

    return f_edge, cover_prob, implied_prob, edge_value, ev, pick_team

def get_target_scoreboard() -> tuple[str, pd.DataFrame]:
    now_us = datetime.now(us_east_tz)
    target_date_us = now_us.strftime("%m/%d/%Y")
    sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=target_date_us)

    # validate
    ALL_TEAMS = static_teams.get_teams()
    valid_team_ids = set(t["id"] for t in ALL_TEAMS)
    valid = False
    if not sb.empty and "HOME_TEAM_ID" in sb.columns:
        sb_filtered = sb[sb["HOME_TEAM_ID"].isin(valid_team_ids)]
        valid = len(sb_filtered) > 0

    if not valid:
        target_date_us = (now_us + timedelta(days=1)).strftime("%m/%d/%Y")
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=target_date_us)

    return target_date_us, sb

def main():
    season = os.environ.get("NBA_SEASON", "2025-26")

    # init DB table
    db_init()

    # build ID->ABBR map
    ALL_TEAMS = static_teams.get_teams()
    id_map = {t["id"]: t["abbreviation"] for t in ALL_TEAMS}
    valid_team_ids = set(id_map.keys())

    # get schedule date
    target_date_us, sb = get_target_scoreboard()
    if sb.empty or "HOME_TEAM_ID" not in sb.columns:
        print(f"[WARN] scoreboard empty for {target_date_us}")
        return

    sb = sb[sb["HOME_TEAM_ID"].isin(valid_team_ids)].copy()
    if sb.empty:
        print(f"[INFO] no valid NBA games for {target_date_us}")
        return

    ps_db = get_player_stats(season=season)

    today_team_ids = sorted(set(sb["HOME_TEAM_ID"].tolist() + sb["VISITOR_TEAM_ID"].tolist()))
    ctx_db = get_team_context(today_team_ids, game_date_us=target_date_us, season=season)

    pinnacle_map = get_pinnacle_odds_for_date(target_date_us)

    auto_rows = []
    for _, row in sb.iterrows():
        h_id, a_id = int(row["HOME_TEAM_ID"]), int(row["VISITOR_TEAM_ID"])
        h_abbr, a_abbr = id_map.get(h_id, str(h_id)), id_map.get(a_id, str(a_id))
        h_cn = TEAM_NAME_CH.get(h_abbr, h_abbr)
        a_cn = TEAM_NAME_CH.get(a_abbr, a_abbr)

        def build_pkg(tid: int):
            ctx = ctx_db.get(tid, {"b2b": False, "recent_w": 0.5})
            # job version: ignore injuries (optional; you can add later)
            active = ps_db[ps_db["TEAM_ID"] == tid].sort_values("IMPACT", ascending=False).copy() if not ps_db.empty else pd.DataFrame()
            return {
                "pts": float(active["PTS"].sum()) if not active.empty and "PTS" in active.columns else 0.0,
                "impact": float(active["IMPACT"].mean()) if not active.empty and "IMPACT" in active.columns else 0.0,
                "b2b": bool(ctx["b2b"]),
                "recent_w": float(ctx["recent_w"]),
            }

        h_p, a_p = build_pkg(h_id), build_pkg(a_id)

        b2b_v = (-2.5 if h_p["b2b"] else 0) - (-2.5 if a_p["b2b"] else 0)
        recent_v = (h_p["recent_w"] - a_p["recent_w"]) * 5
        base_diff = (h_p["pts"] - a_p["pts"]) * 0.09 + (h_p["impact"] - a_p["impact"]) * 3.8 + 2.5 + b2b_v + recent_v

        game_id = f"{a_abbr}_{h_abbr}_{target_date_us.replace('/','')}"
        pin = pinnacle_map.get((a_abbr, h_abbr), None)
        pin_ok = bool(pin and pin.get("ok"))
        sp = float(pin["home_spread"]) if pin_ok else 0.0
        oh = float(pin["home_odds"]) if pin_ok else 1.90
        oa = float(pin["away_odds"]) if pin_ok else 1.90
        src = "Pinnacle ✅" if pin_ok else "Fallback ⚠️"

        f_edge, cover_prob, implied_prob, edge_value, ev, pick_team = compute_metrics(
            base_diff, sp, oh, oa, h_cn, a_cn
        )

        auto_rows.append({
            "game_id": game_id,
            "game_date_us": target_date_us,
            "season": season,
            "away_abbr": a_abbr,
            "home_abbr": h_abbr,
            "away_name": a_cn,
            "home_name": h_cn,

            "home_spread": sp,
            "home_odds": oh,
            "away_odds": oa,
            "line_source": src,

            "base_diff": float(base_diff),
            "f_edge": float(f_edge),
            "cover_prob": float(cover_prob),
            "implied_prob": float(implied_prob),
            "edge_value": float(edge_value),
            "ev": float(ev),
            "pick_team": str(pick_team),

            "status": "scheduled",
            "away_score": None,
            "home_score": None,
            "cover": None,
            "settled_at_tw": None,
        })

    bulk_upsert(auto_rows)
    print(f"[OK] sync date={target_date_us} rows={len(auto_rows)}")

if __name__ == "__main__":
    main()
