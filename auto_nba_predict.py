import streamlit as st
from nba_api.stats.endpoints import scoreboardv2, leaguedashplayerstats, teamgamelog
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, re, unicodedata, time, math
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# -----------------------------
# Supabase(Postgres) driver
# -----------------------------
try:
    import psycopg2
    import psycopg2.extras
except Exception as e:
    psycopg2 = None

# =========================================================
# 1) 核心配置（保留原 UI；強化邏輯與穩定性）
# =========================================================
warnings.filterwarnings("ignore")

tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

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
ALL_TEAMS = teams.get_teams()
VALID_TEAM_IDS = [t["id"] for t in ALL_TEAMS]
ID_MAP = {t["id"]: t["abbreviation"] for t in ALL_TEAMS}

# =========================================================
# 2) 工具：名字正規化 + endpoint 安全抓取（含簡單重試）
# =========================================================
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


def fetch_safe_df(endpoint, retries: int = 2, sleep_s: float = 0.6, **kwargs) -> pd.DataFrame:
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


# =========================================================
# 2.1) 過盤機率映射（保守 + 截斷 + 可被回測校準）
# =========================================================
DEFAULT_PROB_SCALE = 12.0
PROB_FLOOR = 0.10
PROB_CEIL  = 0.90

def sigmoid(x: float) -> float:
    # 避免 overflow
    x = max(min(x, 50), -50)
    return 1.0 / (1.0 + math.exp(-x))

def calc_cover_prob(edge_points: float, prob_scale: float) -> float:
    # edge_points = abs(模型點數優勢 + 主隊盤口)
    x = abs(edge_points) / max(prob_scale, 1e-6)
    p = sigmoid(x)
    # 截斷避免 99% 這種不合理值
    p = min(max(p, PROB_FLOOR), PROB_CEIL)
    return p


# =========================================================
# 3) 賽程抓取（先決定目標日期，再拉賽程）
# =========================================================
def get_target_scoreboard() -> tuple[str, pd.DataFrame]:
    now_us = datetime.now(us_east_tz)
    target_date_us = now_us.strftime("%m/%d/%Y")
    sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=target_date_us)

    valid = False
    if not sb.empty and "HOME_TEAM_ID" in sb.columns:
        sb_filtered = sb[sb["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)]
        valid = len(sb_filtered) > 0

    if not valid:
        target_date_us = (now_us + timedelta(days=1)).strftime("%m/%d/%Y")
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=target_date_us)

    return target_date_us, sb


# =========================================================
# 4) 球員資料（全聯盟）— cache
# =========================================================
@st.cache_data(ttl=3600)
def get_player_stats(season: str = "2025-26") -> pd.DataFrame:
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

    # 避免賽季初樣本太少造成誇張
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


# =========================================================
# 5) 傷病報告（ESPN）— cache
# =========================================================
@st.cache_data(ttl=900)
def get_injuries() -> pd.DataFrame:
    inj_list = []
    try:
        url = "https://www.espn.com/nba/injuries"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        tables = soup.select(".ResponsiveTable") or soup.select("section")

        for table in tables:
            title_el = table.select_one(".Table__Title") or table.find(["h2", "h3"])
            if not title_el:
                continue
            t_name = title_el.get_text(strip=True)
            t_name_norm = t_name.lower()

            # 找隊伍縮寫
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
                raw_reason = cols[-1].get_text(" ", strip=True) if len(cols) >= 3 else "無"

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
        pass

    return pd.DataFrame(inj_list)


# =========================================================
# 6) 隊伍 Context（只針對「今日有賽程」隊伍）— cache
# =========================================================
@st.cache_data(ttl=3600)
def get_team_context(team_ids: list[int], game_date_us: str, season: str = "2025-26") -> dict:
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


# =========================================================
# 7) Supabase(Postgres) 連線與表（Session pooler / IPv4）
# =========================================================
def _require_secrets(keys: list[str]) -> bool:
    missing = [k for k in keys if k not in st.secrets]
    if missing:
        st.error(f"缺少 secrets：{', '.join(missing)}（請到 Streamlit Cloud → Settings → Secrets 設定）")
        return False
    return True

@st.cache_resource
def get_pg_conn():
    if psycopg2 is None:
        raise RuntimeError("缺少 psycopg2，請在 requirements.txt 加入 psycopg2-binary")

    need = ["SUPABASE_HOST","SUPABASE_DB","SUPABASE_USER","SUPABASE_PASSWORD","SUPABASE_PORT"]
    if not _require_secrets(need):
        raise RuntimeError("缺少 Supabase 連線 secrets")

    host = st.secrets["SUPABASE_HOST"]
    db   = st.secrets["SUPABASE_DB"]
    user = st.secrets["SUPABASE_USER"]
    pwd  = st.secrets["SUPABASE_PASSWORD"]
    port = int(st.secrets["SUPABASE_PORT"])

    # Session pooler 一般需要 SSL
    conn = psycopg2.connect(
        host=host,
        dbname=db,
        user=user,
        password=pwd,
        port=port,
        sslmode="require",
    )
    conn.autocommit = True
    return conn

def pg_exec(sql: str, params=None):
    conn = get_pg_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)

def pg_fetch_df(sql: str, params=None) -> pd.DataFrame:
    conn = get_pg_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return pd.DataFrame(rows)

def ensure_tables():
    # 兩張主要表 + 模型參數表（用來校準 prob_scale）
    pg_exec("""
    create table if not exists market_snapshot (
      id bigserial primary key,
      game_key text not null,
      date_us text not null,
      home_abbr text,
      away_abbr text,
      home_spread double precision,
      home_odds double precision,
      away_odds double precision,
      bookmaker text,
      commence_time timestamptz,
      captured_at timestamptz default now(),
      base_diff double precision,
      recent_v double precision,
      b2b_v double precision
    );
    """)
    pg_exec("""create index if not exists idx_market_snapshot_game_key on market_snapshot(game_key);""")
    pg_exec("""create index if not exists idx_market_snapshot_date_us on market_snapshot(date_us);""")

    pg_exec("""
    create table if not exists edge_training_data (
      id bigserial primary key,
      game_key text unique,
      date_us text,
      home_abbr text,
      away_abbr text,
      home_spread double precision,
      home_odds double precision,
      away_odds double precision,
      home_score double precision,
      away_score double precision,
      y double precision,
      base_diff double precision,
      recent_v double precision,
      b2b_v double precision,
      created_at timestamptz default now()
    );
    """)
    pg_exec("""create index if not exists idx_edge_training_date on edge_training_data(date_us);""")

    pg_exec("""
    create table if not exists model_params (
      k text primary key,
      v text,
      updated_at timestamptz default now()
    );
    """)

def set_param(k: str, v: str):
    pg_exec("""
      insert into model_params(k,v,updated_at)
      values(%s,%s,now())
      on conflict (k) do update set v=excluded.v, updated_at=excluded.updated_at;
    """, (k, v))

def get_param(k: str, default: str) -> str:
    df = pg_fetch_df("select v from model_params where k=%s limit 1;", (k,))
    if df.empty:
        return default
    return str(df.iloc[0]["v"])

def get_prob_scale() -> float:
    try:
        return float(get_param("prob_scale", str(DEFAULT_PROB_SCALE)))
    except Exception:
        return DEFAULT_PROB_SCALE


# =========================================================
# 8) Odds API（Pinnacle）快照抓取
# =========================================================
ODDS_SPORT_KEY = "basketball_nba"
ODDS_REGIONS = "us"
ODDS_MARKETS = "spreads,h2h"
ODDS_ODDS_FORMAT = "decimal"
ODDS_DATE_FORMAT = "iso"
PREFERRED_BOOK = "pinnacle"

def odds_api_get(url: str, params: dict) -> dict:
    if not _require_secrets(["ODDS_API_KEY"]):
        return {}
    params = dict(params)
    params["apiKey"] = st.secrets["ODDS_API_KEY"]
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return {}
        return r.json()
    except Exception:
        return {}

def fetch_odds_pinnacle() -> list[dict]:
    # 取得 upcoming games odds（包含 spreads / h2h）
    url = f"https://api.the-odds-api.com/v4/sports/{ODDS_SPORT_KEY}/odds"
    params = {
        "regions": ODDS_REGIONS,
        "markets": ODDS_MARKETS,
        "oddsFormat": ODDS_ODDS_FORMAT,
        "dateFormat": ODDS_DATE_FORMAT,
        "bookmakers": PREFERRED_BOOK,
    }
    data = odds_api_get(url, params)
    if isinstance(data, list):
        return data
    return []

def canonical_team_key(name: str) -> str:
    # 用於 Odds API team name 的粗略比對
    s = (name or "").lower()
    s = re.sub(r"[^a-z\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def build_odds_index(odds_games: list[dict]) -> dict:
    """
    回傳 dict[(home_key, away_key)] = {
      'home_spread': float, 'home_odds': float, 'away_odds': float,
      'commence_time': str, 'bookmaker': 'pinnacle'
    }
    """
    idx = {}
    for g in odds_games:
        home = canonical_team_key(g.get("home_team",""))
        away = canonical_team_key(g.get("away_team",""))
        commence = g.get("commence_time")
        books = g.get("bookmakers", [])
        if not books:
            continue
        book = books[0]
        markets = {m.get("key"): m for m in book.get("markets", [])}

        home_spread = None
        home_odds = None
        away_odds = None

        # spreads：需要找到 home 的 point（主隊盤口），並取對應 price 作主賠；客賠同 market 另一邊
        if "spreads" in markets:
            outcomes = markets["spreads"].get("outcomes", [])
            # outcomes: [{name, point, price}]
            # name 會是 home 或 away team name
            for o in outcomes:
                n = canonical_team_key(o.get("name",""))
                if n == home:
                    try:
                        home_spread = float(o.get("point"))
                    except Exception:
                        home_spread = None
                    try:
                        home_odds = float(o.get("price"))
                    except Exception:
                        home_odds = None
                elif n == away:
                    try:
                        away_odds = float(o.get("price"))
                    except Exception:
                        away_odds = None

        # 如果 spreads 沒帶 odds，h2h 取一下（當備援，雖然不是讓分賠）
        if (home_odds is None or away_odds is None) and "h2h" in markets:
            outcomes = markets["h2h"].get("outcomes", [])
            for o in outcomes:
                n = canonical_team_key(o.get("name",""))
                if n == home and home_odds is None:
                    try:
                        home_odds = float(o.get("price"))
                    except Exception:
                        pass
                if n == away and away_odds is None:
                    try:
                        away_odds = float(o.get("price"))
                    except Exception:
                        pass

        if home_spread is None:
            continue

        idx[(home, away)] = {
            "home_spread": home_spread,
            "home_odds": home_odds,
            "away_odds": away_odds,
            "commence_time": commence,
            "bookmaker": PREFERRED_BOOK,
        }
    return idx

def match_odds_for_game(odds_idx: dict, home_eng: str, away_eng: str) -> dict | None:
    hk = canonical_team_key(home_eng)
    ak = canonical_team_key(away_eng)
    if (hk, ak) in odds_idx:
        return odds_idx[(hk, ak)]
    # 嘗試交換（有些資料源主客可能相反，但 Odds API 一般是 home/away 正確）
    if (ak, hk) in odds_idx:
        # 若反了，代表我們 match 反向，需把 spread 轉換成「我們的主隊」角度
        od = odds_idx[(ak, hk)]
        return {
            "home_spread": -float(od["home_spread"]),
            "home_odds": od.get("away_odds"),
            "away_odds": od.get("home_odds"),
            "commence_time": od.get("commence_time"),
            "bookmaker": od.get("bookmaker"),
        }
    return None

def snapshot_upsert(game_key: str, date_us: str, home_abbr: str, away_abbr: str,
                    home_spread: float, home_odds: float, away_odds: float,
                    bookmaker: str, commence_time,
                    base_diff: float, recent_v: float, b2b_v: float):
    # 同一天同場：只保留「最新一筆」→ 先刪再插
    pg_exec("delete from market_snapshot where game_key=%s and date_us=%s;", (game_key, date_us))
    pg_exec("""
      insert into market_snapshot(game_key,date_us,home_abbr,away_abbr,home_spread,home_odds,away_odds,bookmaker,commence_time,captured_at,base_diff,recent_v,b2b_v)
      values(%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s);
    """, (game_key, date_us, home_abbr, away_abbr, home_spread, home_odds, away_odds, bookmaker, commence_time, base_diff, recent_v, b2b_v))


# =========================================================
# 9) 用訓練資料「校準」prob_scale（避免誇張 99%）
# =========================================================
def calibrate_prob_scale():
    df = pg_fetch_df("""
      select game_key, base_diff, home_spread, home_score, away_score, y
      from edge_training_data
      where y is not null
      order by created_at desc
      limit 1200;
    """)
    if df.empty or len(df) < 80:
        return  # 資料太少不校準

    # y = 1 表主隊過盤；我們實際推薦方向取決於 f_edge
    # correct = 主隊過盤(且我們推薦主) 或 主隊沒過盤(且我們推薦客)
    edges = []
    corrects = []
    for _, r in df.iterrows():
        try:
            base = float(r["base_diff"])
            sp = float(r["home_spread"])
            hs = float(r["home_score"])
            as_ = float(r["away_score"])
        except Exception:
            continue
        f_edge = base + sp
        home_cover = 1.0 if (hs + sp) > as_ else 0.0
        correct = home_cover if f_edge > 0 else (1.0 - home_cover)
        edges.append(abs(f_edge))
        corrects.append(correct)

    if len(edges) < 80:
        return

    # grid search：找讓 brier score 最小的 prob_scale
    best_s = None
    best_brier = 1e9
    for s in [8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 18.0, 20.0]:
        preds = [calc_cover_prob(e, s) for e in edges]
        brier = sum((p - y) ** 2 for p, y in zip(preds, corrects)) / len(preds)
        if brier < best_brier:
            best_brier = brier
            best_s = s

    if best_s is not None:
        set_param("prob_scale", str(best_s))


# =========================================================
# 10) UI 初始化（保留原本配置 + 強制更新按鈕）
# =========================================================
st.set_page_config(page_title="NBA Edge v16.0", layout="wide")

h1, h2 = st.columns([0.8, 0.2])
with h1:
    now_tw_str = datetime.now(tw_tz).strftime("%m/%d %H:%M")
    st.title("🏀 NBA Edge 數據預測系統")
    st.caption(f"台灣現在時間：{now_tw_str}")
with h2:
    if st.button("🔄 強制更新傷病/數據"):
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun()
    with st.popover("💡 判讀指南"):
        st.markdown(
            "**點數優勢**：模型預測分差與盤口的差距（點數）。\n\n"
            "**盤口優勢**：過盤機率 - 損益兩平機率（%）。\n\n"
            "**期望報酬**：以盤口機率估算的長期期望（%）。\n\n"
            "**提醒**：ESPN 列表頁與球員頁可能不同步；若列表頁資訊不足，系統會顯示「待確認」以避免誤判✅。\n\n"
            "**重要**：本系統會自動抓 Pinnacle 快照（Odds API），並在隔天用快照盤口回寫比分做回測校準。"
        )

# Supabase tables
if psycopg2 is None:
    st.error("缺少 psycopg2。請在 requirements.txt 加入 psycopg2-binary 後重新部署。")
    st.stop()

try:
    ensure_tables()
except Exception as e:
    st.error(f"Supabase 連線/建表失敗：{e}")
    st.stop()

prob_scale = get_prob_scale()

with st.spinner("⚡ 正在同步美東數據中心..."):
    target_date_us, sb = get_target_scoreboard()
    ps_db = get_player_stats(season="2025-26")
    inj_db = get_injuries()

if sb.empty or "HOME_TEAM_ID" not in sb.columns:
    st.info("📅 目前抓不到賽程資料（Scoreboard API 回傳空）。請稍後重試。")
    st.stop()

sb_filtered = sb[sb["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)].copy()

if sb_filtered.empty:
    st.info(f"📅 {target_date_us}（美東）無有效 NBA 賽程。")
    st.stop()
else:
    now_us = datetime.now(us_east_tz).strftime("%m/%d/%Y")
    if target_date_us != now_us:
        st.info(f"📅 今日美東無賽程，已為您自動跳轉至明日：{target_date_us}")
    else:
        st.success(f"📅 正在分析美東今日賽程：{target_date_us}")

today_team_ids = sorted(set(sb_filtered["HOME_TEAM_ID"].tolist() + sb_filtered["VISITOR_TEAM_ID"].tolist()))
ctx_db = get_team_context(today_team_ids, game_date_us=target_date_us, season="2025-26")

if inj_db.empty:
    st.warning("⚠️ 傷病名單目前抓不到（ESPN 可能改版或暫時阻擋），推薦將不會排除傷兵。")

# =========================================================
# 11) 主計算：建立每場 pkg + base_diff（保留你的核心公式）
# =========================================================
all_games_data = []
for _, row in sb_filtered.iterrows():
    h_id, a_id = row["HOME_TEAM_ID"], row["VISITOR_TEAM_ID"]
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
            "df": active,
            "inj": t_inj,
            "b2b": bool(ctx["b2b"]),
            "recent_w": float(ctx["recent_w"]),
        }

    h_p, a_p = build_pkg(h_id, h_abbr), build_pkg(a_id, a_abbr)

    b2b_v = (-2.5 if h_p["b2b"] else 0) - (-2.5 if a_p["b2b"] else 0)
    recent_v = (h_p["recent_w"] - a_p["recent_w"]) * 5

    base_diff = (h_p["pts"] - a_p["pts"]) * 0.09 + (h_p["impact"] - a_p["impact"]) * 3.8 + 2.5 + b2b_v + recent_v

    # game_key：用縮寫 + 日期，供快照/回測一致
    game_key = f"{a_abbr}_{h_abbr}_{target_date_us.replace('/','')}"
    a_cn = TEAM_NAME_CH.get(a_abbr, a_abbr)
    h_cn = TEAM_NAME_CH.get(h_abbr, h_abbr)

    all_games_data.append(
        {
            "game_key": game_key,
            "label": f"{a_cn}(客) @ {h_cn}(主)",
            "base_diff": float(base_diff),
            "recent_v": float(recent_v),
            "b2b_v": float(b2b_v),
            "h_pkg": h_p,
            "a_pkg": a_p,
            "h_cn": h_cn,
            "a_cn": a_cn,
            "h_abbr": h_abbr,
            "a_abbr": a_abbr,
            "h_eng": TEAM_MAP.get(h_abbr, [h_abbr])[0],
            "a_eng": TEAM_MAP.get(a_abbr, [a_abbr])[0],
        }
    )

# =========================================================
# 12) 自動抓 Pinnacle 快照並寫入 Supabase（每天啟動自動做）
# =========================================================
# 為避免每次 rerun 都打 API，做簡單「每日快照鎖」
today_lock_key = f"snapshot_done_{target_date_us}"
if today_lock_key not in st.session_state:
    st.session_state[today_lock_key] = False

def auto_snapshot_to_supabase():
    # 只做一次（此 session）
    if st.session_state.get(today_lock_key):
        return

    odds_games = fetch_odds_pinnacle()
    odds_idx = build_odds_index(odds_games)

    wrote = 0
    for g in all_games_data:
        od = match_odds_for_game(odds_idx, g["h_eng"], g["a_eng"])
        if od is None:
            continue

        hs = od.get("home_spread")
        ho = od.get("home_odds") if od.get("home_odds") is not None else 1.90
        ao = od.get("away_odds") if od.get("away_odds") is not None else 1.90

        try:
            snapshot_upsert(
                game_key=g["game_key"],
                date_us=target_date_us,
                home_abbr=g["h_abbr"],
                away_abbr=g["a_abbr"],
                home_spread=float(hs),
                home_odds=float(ho),
                away_odds=float(ao),
                bookmaker=od.get("bookmaker", PREFERRED_BOOK),
                commence_time=od.get("commence_time"),
                base_diff=g["base_diff"],
                recent_v=g["recent_v"],
                b2b_v=g["b2b_v"],
            )
            wrote += 1
        except Exception:
            pass

    st.session_state[today_lock_key] = True
    if wrote > 0:
        st.caption(f"✅ 已自動寫入 Pinnacle 快照到 Supabase：{wrote} 場（{target_date_us}）")
    else:
        st.caption("⚠️ 本次未寫入 Pinnacle 快照（可能 Odds API 無資料/超額/隊名匹配失敗）。")

# 自動快照（不打擾 UI）
auto_snapshot_to_supabase()

# =========================================================
# 13) 隔天更新：把已結束比賽寫入訓練表 + 校準
# =========================================================
def update_yesterday_results_and_train():
    # 昨天（美東）
    y_us = (datetime.now(us_east_tz) - timedelta(days=1)).strftime("%m/%d/%Y")
    sb_y = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=y_us)
    if sb_y.empty or "HOME_TEAM_ID" not in sb_y.columns:
        st.warning("抓不到昨天賽果（Scoreboard API 可能延遲或空）。")
        return

    sb_y = sb_y[sb_y["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)].copy()
    if sb_y.empty:
        st.info(f"昨天（美東 {y_us}）沒有有效賽事。")
        return

    # ScoreboardV2 的欄位有時不同，嘗試抓分數
    # 常見欄位：HOME_TEAM_ID, VISITOR_TEAM_ID, HOME_TEAM_SCORE, VISITOR_TEAM_SCORE
    hs_col = None
    as_col = None
    for c in sb_y.columns:
        if "HOME_TEAM_SCORE" == c:
            hs_col = c
        if "VISITOR_TEAM_SCORE" == c:
            as_col = c
    if hs_col is None or as_col is None:
        # fallback: 常見也可能叫 PTS_HOME / PTS_AWAY（視 API）
        cand_h = [c for c in sb_y.columns if "HOME" in c and "SCORE" in c]
        cand_a = [c for c in sb_y.columns if ("VISITOR" in c or "AWAY" in c) and "SCORE" in c]
        hs_col = cand_h[0] if cand_h else None
        as_col = cand_a[0] if cand_a else None

    if hs_col is None or as_col is None:
        st.warning("昨天賽果欄位無法解析（API 欄位變動）。")
        return

    wrote = 0
    for _, r in sb_y.iterrows():
        hid = r["HOME_TEAM_ID"]
        aid = r["VISITOR_TEAM_ID"]
        h_abbr = ID_MAP.get(hid, str(hid))
        a_abbr = ID_MAP.get(aid, str(aid))
        game_key = f"{a_abbr}_{h_abbr}_{y_us.replace('/','')}"

        try:
            home_score = float(r[hs_col])
            away_score = float(r[as_col])
        except Exception:
            continue

        # 讀昨天快照（若沒有，就跳過：避免用假盤訓練）
        snap = pg_fetch_df("""
          select *
          from market_snapshot
          where game_key=%s and date_us=%s
          order by captured_at desc
          limit 1;
        """, (game_key, y_us))

        if snap.empty:
            continue

        sp = float(snap.iloc[0].get("home_spread") or 0.0)
        ho = float(snap.iloc[0].get("home_odds") or 1.90)
        ao = float(snap.iloc[0].get("away_odds") or 1.90)
        base = float(snap.iloc[0].get("base_diff") or 0.0)
        recent_v = float(snap.iloc[0].get("recent_v") or 0.0)
        b2b_v = float(snap.iloc[0].get("b2b_v") or 0.0)

        y = 1.0 if (home_score + sp) > away_score else 0.0

        # upsert 到訓練表（game_key unique）
        pg_exec("""
          insert into edge_training_data(
            game_key,date_us,home_abbr,away_abbr,
            home_spread,home_odds,away_odds,
            home_score,away_score,y,
            base_diff,recent_v,b2b_v,created_at
          ) values(
            %s,%s,%s,%s,
            %s,%s,%s,
            %s,%s,%s,
            %s,%s,%s,now()
          )
          on conflict (game_key) do update set
            home_spread=excluded.home_spread,
            home_odds=excluded.home_odds,
            away_odds=excluded.away_odds,
            home_score=excluded.home_score,
            away_score=excluded.away_score,
            y=excluded.y,
            base_diff=excluded.base_diff,
            recent_v=excluded.recent_v,
            b2b_v=excluded.b2b_v,
            created_at=now();
        """, (game_key, y_us, h_abbr, a_abbr, sp, ho, ao, home_score, away_score, y, base, recent_v, b2b_v))

        wrote += 1

    if wrote == 0:
        st.warning("昨天賽果未寫入：可能昨天沒有快照（Odds API 沒抓到/尚未啟動 app）。")
        return

    # 校準機率曲線（prob_scale）
    calibrate_prob_scale()
    st.success(f"✅ 已更新昨天賽果並寫入訓練資料：{wrote} 場；已嘗試校準機率曲線。")

# 更新按鈕（保留你的「一天按一次」概念）
if st.button("📥 更新已結束比賽到訓練資料並校準"):
    with st.spinner("正在更新昨天賽果並校準..."):
        update_yesterday_results_and_train()
        st.rerun()

# 更新後重新讀 prob_scale
prob_scale = get_prob_scale()

# =========================================================
# 14) 取「市場快照」當預設值（讓 UI 一打開就貼近真盤）
# =========================================================
snap_today = pg_fetch_df("""
  select game_key, home_spread, home_odds, away_odds
  from market_snapshot
  where date_us=%s;
""", (target_date_us,))
snap_map = {}
if not snap_today.empty:
    for _, r in snap_today.iterrows():
        snap_map[str(r["game_key"])] = {
            "sp": float(r.get("home_spread") or 0.0),
            "ho": float(r.get("home_odds") or 1.90),
            "ao": float(r.get("away_odds") or 1.90),
        }

# =========================================================
# 15) 🔥 今日最能買（至多三場）— 依挑場規則
# =========================================================
EDGE_THRESHOLD = 0.05
MAX_PICKS = 3
MAX_GAMES_FOR_PICK = 10

def get_market_defaults(g):
    # 先用 session_state（使用者手改過）→ 再用 supabase 快照 → 最後預設
    key = g["game_key"]
    if f"sp_{key}" in st.session_state:
        sp = float(st.session_state.get(f"sp_{key}", 0.0))
        ho = float(st.session_state.get(f"oh_{key}", 1.90))
        ao = float(st.session_state.get(f"oa_{key}", 1.90))
        return sp, ho, ao

    if key in snap_map:
        return snap_map[key]["sp"], snap_map[key]["ho"], snap_map[key]["ao"]

    return 0.0, 1.90, 1.90

pick_pool = []
for g in all_games_data[:MAX_GAMES_FOR_PICK]:
    u_sp, u_oh, u_oa = get_market_defaults(g)

    f_edge = g["base_diff"] + u_sp
    cover_prob = calc_cover_prob(abs(f_edge), prob_scale)

    pick_side = g["h_cn"] if f_edge > 0 else g["a_cn"]
    odds = u_oh if f_edge > 0 else u_oa
    implied_prob = 1.0 / odds if odds and odds > 0 else 1.0

    edge_value = cover_prob - implied_prob

    pick_pool.append({
        "g": g,
        "pick_side": pick_side,
        "cover_prob": cover_prob,
        "implied_prob": implied_prob,
        "edge_value": edge_value,
        "edge_points": abs(f_edge),
        "odds": odds,
        "home_spread_input": u_sp,
    })

qualified = [x for x in pick_pool if x["edge_value"] > EDGE_THRESHOLD]
qualified.sort(key=lambda x: (x["cover_prob"], x["edge_value"]), reverse=True)
picks = qualified[:MAX_PICKS]

st.header("🔥 今日過盤推薦 (Top 4)")
if len(picks) == 0:
    st.info("依挑場規則：前 10 場中沒有任何一場「盤口優勢 > 5%」，建議不買、不硬湊。")
else:
    if len(picks) == 1:
        st.success("🎯 今日只有 1 場符合「盤口優勢 > 5%」：建議只買單場（或分注單場），不要硬湊串關。")
    else:
        st.success(f"🎯 今日最能買：已依規則挑出 {len(picks)} 場（最多三場）。")

    cols = st.columns(len(picks))
    for idx, item in enumerate(picks):
        g = item["g"]
        with cols[idx]:
            with st.container(border=True):
                st.subheader(f"精選 {idx+1}")
                st.write(f"**{g['label']}**")
                st.success(f"首選：{item['pick_side']}")
                st.write(
                    f"過盤機率：**{item['cover_prob']*100:.1f}%** | "
                    f"損益兩平：**{item['implied_prob']*100:.1f}%**"
                )
                st.metric("盤口優勢", f"{item['edge_value']*100:+.1f}%")
                st.write(f"主隊盤口：**{item['home_spread_input']}** | 賠率：**{item['odds']:.2f}**")
                st.write(f"點數優勢：**{item['edge_points']:.1f}**")

st.caption(f"（機率曲線校準參數 prob_scale：{prob_scale:.1f}；資料越多越穩）")

st.divider()

# =========================================================
# 16) 🎯 全部場次與實時計算（保留原 UI；主隊盤口輸入規則）
# =========================================================
st.header("🎯 全部場次與實時計算")

for i in range(0, len(all_games_data), 3):
    cols = st.columns(3)
    for j, g in enumerate(all_games_data[i : i + 3]):
        with cols[j]:
            with st.container(border=True):
                st.subheader(g["label"])

                gid = g["game_key"]
                d_sp, d_oh, d_oa = get_market_defaults(g)

                u_sp = st.number_input(
                    "主隊盤口（主讓分填負｜主受讓填正）",
                    min_value=-60.0,
                    max_value=60.0,
                    value=float(d_sp),
                    step=0.5,
                    key=f"sp_{gid}",
                )
                u_oh = st.number_input(
                    "主賠",
                    min_value=1.01,
                    max_value=5.0,
                    value=float(d_oh),
                    step=0.01,
                    key=f"oh_{gid}",
                )
                u_oa = st.number_input(
                    "客賠",
                    min_value=1.01,
                    max_value=5.0,
                    value=float(d_oa),
                    step=0.01,
                    key=f"oa_{gid}",
                )

                f_edge = g["base_diff"] + u_sp
                cover_prob = calc_cover_prob(abs(f_edge), prob_scale)
                rec = g["h_cn"] if f_edge > 0 else g["a_cn"]
                odds = u_oh if f_edge > 0 else u_oa

                implied_prob = 1.0 / odds if odds and odds > 0 else 1.0
                edge_value = cover_prob - implied_prob
                ev = (cover_prob * odds) - 1

                st.write(f"過盤機率：**{cover_prob*100:.1f}%** | 點數優勢：**{abs(f_edge):.1f}**")
                st.write(f"盤口優勢：**{edge_value*100:+.1f}%** | 期望報酬：**{ev*100:+.1f}%**")

                if edge_value > EDGE_THRESHOLD:
                    st.success(f"🔥 符合挑場門檻（盤口優勢 > 5%）：{rec}")
                else:
                    st.info(f"建議：{rec}")

# =========================================================
# 17) 🔍 深度查詢（保留原 UI）
# =========================================================
st.divider()
st.header("🔍 深度數據查詢")

sel = st.selectbox("請選擇場次", [g["label"] for g in all_games_data])
if sel:
    curr = next(g for g in all_games_data if g["label"] == sel)

    st.write(
        f"📊 **戰前速報**："
        f"{'🚨 客隊背靠背' if curr['a_pkg']['b2b'] else '✅ 客隊體能正常'} | "
        f"{'🚨 主隊背靠背' if curr['h_pkg']['b2b'] else '✅ 主隊體能正常'}"
    )

    c1, c2 = st.columns(2)
    for col, pkg, side in zip([c1, c2], [curr["h_pkg"], curr["a_pkg"]], ["(主)", "(客)"]):
        with col:
            team_name = curr["h_cn"] if side == "(主)" else curr["a_cn"]
            st.subheader(f"{team_name} {side}")
            st.write(f"近五場勝率: **{pkg['recent_w']*100:.0f}%**")

            if pkg["df"] is not None and not pkg["df"].empty:
                show_cols = [c for c in ["PLAYER_NAME", "PTS", "IMPACT"] if c in pkg["df"].columns]
                st.dataframe(pkg["df"][show_cols].head(12), hide_index=True)
            else:
                st.write("（球員資料不足或 API 暫時不可用）")

            if pkg["inj"] is not None and not pkg["inj"].empty:
                st.dataframe(pkg["inj"][["球員", "狀態", "原因"]], hide_index=True)
            else:
                st.write("✅ 無傷病報告")
