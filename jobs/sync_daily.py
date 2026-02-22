import os, re, time, math, unicodedata, warnings
from datetime import datetime, timedelta
import pytz
import pandas as pd
import requests

from nba_api.stats.endpoints import scoreboardv3, leaguedashplayerstats, teamgamelog
from nba_api.stats.static import teams as static_teams

import psycopg2
from psycopg2.extras import execute_values

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

warnings.filterwarnings("ignore")

# =========================
# Timezones
# =========================
tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

# =========================
# Team maps (same as your app)
# =========================
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

ALL_TEAMS = static_teams.get_teams()
VALID_TEAM_IDS = set(t["id"] for t in ALL_TEAMS)
ID_MAP = {t["id"]: t["abbreviation"] for t in ALL_TEAMS}

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

# =========================
# Probability mapping (same philosophy)
# =========================
PROB_SCALE = 12.0
PROB_FLOOR = 0.12
PROB_CEIL  = 0.88

def calc_cover_prob(edge_points: float) -> float:
    x = abs(edge_points) / PROB_SCALE
    p = 1.0 / (1.0 + math.exp(-x))
    p = max(PROB_FLOOR, min(PROB_CEIL, p))
    return p

# =========================
# Utils
# =========================
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

def fetch_safe_df(endpoint_cls, retries: int = 2, sleep_s: float = 0.8, **kwargs) -> pd.DataFrame:
    for attempt in range(retries + 1):
        try:
            r = endpoint_cls(**kwargs).get_dict()
            # nba_api resultSets can vary; take the first table-like set
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

# =========================
# DB
# =========================
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

        status TEXT,            -- scheduled/in_progress/final
        away_score INTEGER,
        home_score INTEGER,
        cover INTEGER,          -- 1=home cover, 0=not, 2=push, NULL=unknown
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

# =========================
# Data fetchers
# =========================
def fetch_scoreboard(date_us: str) -> pd.DataFrame:
    # ScoreboardV3 is recommended (V2 has deprecation issues)
    sb = fetch_safe_df(scoreboardv3.ScoreboardV3, game_date=date_us, retries=2, sleep_s=0.9)
    if sb.empty or "HOME_TEAM_ID" not in sb.columns:
        return pd.DataFrame()
    sb = sb[sb["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)].copy()
    return sb

def get_player_stats(season: str) -> pd.DataFrame:
    ps = fetch_safe_df(
        leaguedashplayerstats.LeagueDashPlayerStats,
        season=season,
        per_mode_detailed="PerGame",
        retries=2,
        sleep_s=0.9,
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
        log = fetch_safe_df(teamgamelog.TeamGameLog, team_id=tid, season=season, retries=2, sleep_s=0.9)
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

def get_injuries() -> pd.DataFrame:
    """
    ESPN injuries. Optional: if bs4 not installed or ESPN blocks, return empty.
    """
    if BeautifulSoup is None:
        return pd.DataFrame()

    inj_list = []
    try:
        url = "https://www.espn.com/nba/injuries"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        tables = soup.select(".ResponsiveTable") or soup.select("section")

        for table in tables:
            title_el = table.select_one(".Table__Title") or table.find(["h2", "h3"])
            if not title_el:
                continue
            t_name = title_el.get_text(strip=True)
            t_name_norm = t_name.lower()

            t_abbr = None
            for abbr, info in TEAM_MAP.items():
                if info[0].lower() in t_name_norm:
                    t_abbr = abbr
                    break
            if not t_abbr:
                for abbr, info in TEAM_MAP.items():
                    eng_tokens = [w for w in info[0].lower().split() if len(w) >= 3]
                    if any(tok in t_name_norm for tok in eng_tokens):
                        t_abbr = abbr
                        break
            if not t_abbr:
                continue

            rows = table.select("tbody tr") if table.select("tbody tr") else table.select("tr")
            for r in rows:
                cols = r.select("td")
                if len(cols) < 2:
                    continue

                raw_player = cols[0].get_text(" ", strip=True)
                raw_player = re.sub(r"\s+(PG|SG|SF|PF|C|G|F)\s*$", "", raw_player, flags=re.I).strip()

                row_text = " | ".join([c.get_text(" ", strip=True) for c in cols]).lower()
                raw_reason = cols[-1].get_text(" ", strip=True) if len(cols) >= 3 else "N/A"

                out_kw = ["out", "ruled out", "will not play", "inactive", "suspended"]
                q_kw   = ["questionable", "doubtful", "gtd", "day-to-day", "game time decision"]
                ok_kw  = ["available", "will play", "probable"]

                is_out = any(k in row_text for k in out_kw)
                is_q   = any(k in row_text for k in q_kw)
                is_ok  = any(k in row_text for k in ok_kw)

                if is_out:
                    status_cn = "❌ [確定缺陣]"
                elif is_q:
                    status_cn = "📋 [觀察名單]"
                elif is_ok:
                    status_cn = "✅ [預計出賽]"
                else:
                    status_cn = "📋 [資訊不足/待確認]"

                inj_list.append(
                    {
                        "NORM": norm_name(raw_player),
                        "球員": raw_player,
                        "狀態": status_cn,
                        "原因": raw_reason,
                        "球隊": t_abbr,
                        "IS_OUT": bool(is_out),
                    }
                )
    except Exception:
        return pd.DataFrame()

    return pd.DataFrame(inj_list)

def get_pinnacle_odds_for_date(game_date_us: str) -> dict:
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

            bk = None
            for b in books:
                if norm_name(b.get("key", "")) == "pinnacle":
                    bk = b
                    break
            if not bk:
                for b in books:
                    if "pinnacle" in norm_name(b.get("title", "")):
                        bk = b
                        break
            if not bk:
                continue

            mkts = bk.get("markets", [])
            spreads = None
            for m in mkts:
                if m.get("key") == "spreads":
                    spreads = m
                    break
            if not spreads:
                continue

            outcomes = spreads.get("outcomes", [])
            if len(outcomes) < 2:
                continue

            home_spread = None
            home_odds = None
            away_odds = None

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

# =========================
# Model metrics
# =========================
def compute_metrics(base_diff: float, home_spread_input: float, home_odds: float, away_odds: float, home_name_cn: str, away_name_cn: str):
    f_edge = float(base_diff) + float(home_spread_input)
    cover_prob = calc_cover_prob(f_edge)

    pick_team = home_name_cn if f_edge > 0 else away_name_cn
    odds = float(home_odds) if f_edge > 0 else float(away_odds)

    implied_prob = 1.0 / odds if odds and odds > 0 else 1.0
    edge_value = cover_prob - implied_prob
    ev = (cover_prob * odds) - 1.0

    return {
        "f_edge": float(f_edge),
        "cover_prob": float(cover_prob),
        "implied_prob": float(implied_prob),
        "edge_value": float(edge_value),
        "ev": float(ev),
        "pick_team": str(pick_team),
    }

# =========================
# Build + sync rows for one date
# =========================
def sync_one_date(game_date_us: str, season: str, ps_db: pd.DataFrame, inj_db: pd.DataFrame):
    sb = fetch_scoreboard(game_date_us)
    if sb.empty:
        print(f"[WARN] scoreboard empty for {game_date_us}")
        return 0

    sb_filtered = sb.copy()
    today_team_ids = sorted(set(sb_filtered["HOME_TEAM_ID"].tolist() + sb_filtered["VISITOR_TEAM_ID"].tolist()))
    ctx_db = get_team_context(today_team_ids, game_date_us=game_date_us, season=season)
    pin_map = get_pinnacle_odds_for_date(game_date_us)

    rows_to_upsert = []

    for _, row in sb_filtered.iterrows():
        h_id, a_id = int(row["HOME_TEAM_ID"]), int(row["VISITOR_TEAM_ID"])
        h_abbr, a_abbr = ID_MAP.get(h_id, str(h_id)), ID_MAP.get(a_id, str(a_id))

        def build_pkg(tid: int, abbr: str):
            ctx = ctx_db.get(tid, {"b2b": False, "recent_w": 0.5})

            t_inj = inj_db[inj_db["球隊"] == abbr] if not inj_db.empty else pd.DataFrame()
            out_list = t_inj[t_inj["IS_OUT"]]["NORM"].tolist() if not t_inj.empty else []

            if not ps_db.empty and "TEAM_ID" in ps_db.columns and "NORM" in ps_db.columns:
                active = (
                    ps_db[(ps_db["TEAM_ID"] == tid) & (~ps_db["NORM"].isin(out_list))]
                    .sort_values("IMPACT", ascending=False)
                    .copy()
                )
            else:
                active = pd.DataFrame()

            return {
                "pts": float(active["PTS"].sum()) if not active.empty and "PTS" in active.columns else 0.0,
                "impact": float(active["IMPACT"].mean()) if not active.empty and "IMPACT" in active.columns else 0.0,
                "b2b": bool(ctx["b2b"]),
                "recent_w": float(ctx["recent_w"]),
            }

        h_p, a_p = build_pkg(h_id, h_abbr), build_pkg(a_id, a_abbr)

        b2b_v = (-2.5 if h_p["b2b"] else 0) - (-2.5 if a_p["b2b"] else 0)
        recent_v = (h_p["recent_w"] - a_p["recent_w"]) * 5
        base_diff = (h_p["pts"] - a_p["pts"]) * 0.09 + (h_p["impact"] - a_p["impact"]) * 3.8 + 2.5 + b2b_v + recent_v

        game_id = f"{a_abbr}_{h_abbr}_{game_date_us.replace('/','')}"
        a_cn = TEAM_NAME_CH.get(a_abbr, a_abbr)
        h_cn = TEAM_NAME_CH.get(h_abbr, h_abbr)

        pin = pin_map.get((a_abbr, h_abbr))
        pin_ok = bool(pin and pin.get("ok"))
        sp = float(pin["home_spread"]) if pin_ok else 0.0
        oh = float(pin["home_odds"]) if pin_ok else 1.90
        oa = float(pin["away_odds"]) if pin_ok else 1.90
        src = "Pinnacle ✅" if pin_ok else "Fallback ⚠️"

        m = compute_metrics(base_diff, sp, oh, oa, h_cn, a_cn)

        rows_to_upsert.append({
            "game_id": game_id,
            "game_date_us": game_date_us,
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
            "f_edge": float(m["f_edge"]),
            "cover_prob": float(m["cover_prob"]),
            "implied_prob": float(m["implied_prob"]),
            "edge_value": float(m["edge_value"]),
            "ev": float(m["ev"]),
            "pick_team": str(m["pick_team"]),

            "status": "scheduled",
            "away_score": None,
            "home_score": None,
            "cover": None,
            "settled_at_tw": None,
        })

    bulk_upsert(rows_to_upsert)
    return len(rows_to_upsert)

# =========================
# Main
# =========================
def main():
    season = (os.environ.get("NBA_SEASON") or "2025-26").strip()
    db_init()

    # Fetch shared data once per run
    ps_db = get_player_stats(season=season)
    inj_db = get_injuries()

    # ✅ Best practice for snapshot DB: write "US today + US tomorrow"
    today_us = datetime.now(us_east_tz).date()
    dates = [
        today_us.strftime("%m/%d/%Y"),
        (today_us + timedelta(days=1)).strftime("%m/%d/%Y"),
    ]

    total = 0
    for d in dates:
        n = sync_one_date(d, season=season, ps_db=ps_db, inj_db=inj_db)
        print(f"[OK] synced date_us={d}, rows={n}")
        total += n

    print(f"[DONE] total_rows_upserted={total}")

if __name__ == "__main__":
    main()
