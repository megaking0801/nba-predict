import streamlit as st
from nba_api.stats.endpoints import scoreboardv2, leaguedashplayerstats, teamgamelog
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, re, unicodedata, time, math
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

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

# Odds API 端常見隊名 → 我們的縮寫（盡量涵蓋變形）
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
# 2.1) 過盤機率映射（更保守：12%~88%）
# =========================================================
PROB_SCALE = 12.0
PROB_FLOOR = 0.12
PROB_CEIL  = 0.88

def calc_cover_prob(edge_points: float) -> float:
    # logistic，但做硬性截斷（避免“穩賺不賠”的假象）
    x = abs(edge_points) / PROB_SCALE
    p = 1.0 / (1.0 + math.exp(-x))
    if p < PROB_FLOOR:
        p = PROB_FLOOR
    if p > PROB_CEIL:
        p = PROB_CEIL
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

    # 避免季初/小樣本造成“神準假象”
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
# 5) 傷病報告（ESPN）— cache（更保守判讀）
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
# 7) Odds API（Pinnacle）抓盤口/賠率 — cache
#    目標：帶入「預設值」+ 只用真盤當候選池
# =========================================================
@st.cache_data(ttl=900)
def get_pinnacle_odds_for_date(game_date_us: str) -> dict:
    """
    回傳 dict keyed by (away_abbr, home_abbr):
      {("BOS","LAL"): {"home_spread": -3.5, "home_odds": 1.91, "away_odds": 1.91, "ok": True}}
    """
    api_key = None
    try:
        api_key = st.secrets.get("ODDS_API_KEY", None)
    except Exception:
        api_key = None

    if not api_key:
        return {}

    # Odds API 通常用 ISO 日期（YYYY-MM-DD）
    # 用美東日期的當天 00:00 來推
    dt = datetime.strptime(game_date_us, "%m/%d/%Y").date()
    iso_date = dt.strftime("%Y-%m-%d")

    # The Odds API v4 常見：/sports/basketball_nba/odds
    # markets=spreads，bookmakers=pinnacle，oddsFormat=decimal
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

    # 只取該日期（美東日期附近）— 這裡做「日期包含」而不是嚴格等於，避免時區差
    for g in data:
        try:
            commence = g.get("commence_time", "")
            if iso_date not in commence:
                # 若時區落差，commence_time 可能是前一天/後一天 UTC
                # 這裡放寬：同一週期內先不硬濾，交給隊名配對
                pass

            home_name = norm_name(g.get("home_team", ""))
            away_name = norm_name(g.get("away_team", ""))

            home_abbr = ODDS_TEAMNAME_TO_ABBR.get(home_name)
            away_abbr = ODDS_TEAMNAME_TO_ABBR.get(away_name)
            if not home_abbr or not away_abbr:
                continue

            books = g.get("bookmakers", [])
            if not books:
                continue

            # 只抓 pinnacle spreads
            bk = None
            for b in books:
                if norm_name(b.get("key", "")) == "pinnacle":
                    bk = b
                    break
            if not bk:
                # 有些回傳 key 不是 pinnacle，但 title 是 Pinnacle
                for b in books:
                    if "pinnacle" in norm_name(b.get("title", "")):
                        bk = b
                        break
            if not bk:
                continue

            mkts = bk.get("markets", [])
            if not mkts:
                continue

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

            # outcomes 會包含 home/away 各自 point + price
            # 我們要回傳「home_spread（主隊讓分為負）」+ 主客賠率
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
                    # 若 API 給的是主隊 -3.5 就是 -3.5
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


# =========================================================
# 8) UI 初始化（保留原本配置 + 強制更新按鈕）
# =========================================================
st.set_page_config(page_title="NBA Edge v16.0", layout="wide")

h1, h2 = st.columns([0.8, 0.2])
with h1:
    now_tw_str = datetime.now(tw_tz).strftime("%m/%d %H:%M")
    st.title("🏀 NBA Edge 數據預測系統")
    st.caption(f"台灣現在時間：{now_tw_str}")
with h2:
    if st.button("🔄 強制更新（傷病/盤口/數據）"):
        st.cache_data.clear()
        st.rerun()
    with st.popover("💡 判讀指南"):
        st.markdown(
            "**點數優勢**：模型預測分差與盤口的差距（點數）。\n\n"
            "**盤口優勢**：過盤機率 - 損益兩平機率（%）。\n\n"
            "**期望報酬**：以過盤機率估算的長期期望（%）。\n\n"
            "**Top picks（你選 2）**：\n"
            "- 只用 Pinnacle 有抓到 spreads 的場次當候選池（避免假盤）\n"
            "- 但排序/EV/edge_value 用你手動輸入的運彩盤口/賠率重新計算\n\n"
            "**提醒**：若你看到某場主隊盤口=0、賠率=1.90 且來源顯示 Fallback，代表沒有真盤資料，那場不會進 Top picks。"
        )

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
    now_us_str = datetime.now(us_east_tz).strftime("%m/%d/%Y")
    if target_date_us != now_us_str:
        st.info(f"📅 今日美東無賽程，已為您自動跳轉至明日：{target_date_us}")
    else:
        st.success(f"📅 正在分析美東今日賽程：{target_date_us}")

today_team_ids = sorted(set(sb_filtered["HOME_TEAM_ID"].tolist() + sb_filtered["VISITOR_TEAM_ID"].tolist()))
ctx_db = get_team_context(today_team_ids, game_date_us=target_date_us, season="2025-26")

if inj_db.empty:
    st.warning("⚠️ 傷病名單目前抓不到（ESPN 可能改版或暫時阻擋），推薦將不會排除傷兵。")

# Pinnacle odds
pinnacle_map = get_pinnacle_odds_for_date(target_date_us)

# =========================================================
# 9) 主計算：建立每場 pkg + base_diff（保留你的核心公式）
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

    game_id = f"{a_abbr}_{h_abbr}_{target_date_us.replace('/','')}"
    a_cn = TEAM_NAME_CH.get(a_abbr, a_abbr)
    h_cn = TEAM_NAME_CH.get(h_abbr, h_abbr)

    # Pinnacle default for this matchup
    pin = pinnacle_map.get((a_abbr, h_abbr), None)
    pin_ok = bool(pin and pin.get("ok"))
    pin_home_sp = float(pin["home_spread"]) if pin_ok else 0.0
    pin_home_od = float(pin["home_odds"]) if pin_ok else 1.90
    pin_away_od = float(pin["away_odds"]) if pin_ok else 1.90

    all_games_data.append(
        {
            "game_id": game_id,
            "label": f"{a_cn}(客) @ {h_cn}(主)",
            "base_diff": float(base_diff),
            "h_pkg": h_p,
            "a_pkg": a_p,
            "h_cn": h_cn,
            "a_cn": a_cn,
            "h_abbr": h_abbr,
            "a_abbr": a_abbr,
            "pin_ok": pin_ok,
            "pin_home_sp": pin_home_sp,
            "pin_home_od": pin_home_od,
            "pin_away_od": pin_away_od,
        }
    )

# =========================================================
# 10) 挑場規則（你指定的）：候選池=真盤；排序=你手動輸入的運彩
# =========================================================
EDGE_THRESHOLD = 0.05
MAX_PICKS = 3
MAX_GAMES_FOR_PICK = 10

def safe_float(x, default):
    try:
        return float(x)
    except Exception:
        return float(default)

def get_market_inputs_for_game(g):
    gid = g["game_id"]
    # 若使用者沒輸入過，預設帶 Pinnacle（抓不到才 fallback）
    sp_default = g["pin_home_sp"]
    oh_default = g["pin_home_od"]
    oa_default = g["pin_away_od"]

    sp = safe_float(st.session_state.get(f"sp_{gid}", sp_default), sp_default)
    oh = safe_float(st.session_state.get(f"oh_{gid}", oh_default), oh_default)
    oa = safe_float(st.session_state.get(f"oa_{gid}", oa_default), oa_default)

    # 判斷是否手動改過（跟 Pinnacle default 不同即視為手動）
    manual = (abs(sp - sp_default) > 1e-9) or (abs(oh - oh_default) > 1e-9) or (abs(oa - oa_default) > 1e-9)

    if manual:
        src = "手動（運彩）✍️"
    elif g["pin_ok"]:
        src = "Pinnacle ✅"
    else:
        src = "Fallback ⚠️"

    return float(sp), float(oh), float(oa), src, manual

def compute_metrics(g, home_spread_input, home_odds, away_odds):
    # f_edge：你的模型點差（home vs away） + 主隊盤口（主讓負、主受讓正）
    f_edge = g["base_diff"] + home_spread_input

    cover_prob = calc_cover_prob(f_edge)

    # 推薦邊：f_edge > 0 推主隊，否則客隊
    pick_team = g["h_cn"] if f_edge > 0 else g["a_cn"]
    odds = home_odds if f_edge > 0 else away_odds

    implied_prob = 1.0 / odds if odds and odds > 0 else 1.0
    edge_value = cover_prob - implied_prob
    ev = (cover_prob * odds) - 1

    return {
        "f_edge": float(f_edge),
        "edge_points": float(abs(f_edge)),
        "cover_prob": float(cover_prob),
        "implied_prob": float(implied_prob),
        "edge_value": float(edge_value),
        "ev": float(ev),
        "pick_team": pick_team,
        "odds_used": float(odds),
    }

# =========================================================
# 11) 🔥 今日最能買（至多三場）— 依挑場規則（候選池=真盤；排序=手動值）
# =========================================================
# 只取前 10 場
pool_games = all_games_data[:MAX_GAMES_FOR_PICK]

pick_pool = []
for g in pool_games:
    # 候選池：必須 Pinnacle 真盤 OK
    if not g["pin_ok"]:
        continue

    u_sp, u_oh, u_oa, src, manual = get_market_inputs_for_game(g)
    m = compute_metrics(g, u_sp, u_oh, u_oa)
    pick_pool.append({
        "g": g,
        "src": src,
        "manual": manual,
        "home_spread_input": u_sp,
        "home_odds": u_oh,
        "away_odds": u_oa,
        **m
    })

qualified = [x for x in pick_pool if x["edge_value"] > EDGE_THRESHOLD]
qualified.sort(key=lambda x: (x["cover_prob"], x["edge_value"], x["ev"]), reverse=True)
picks = qualified[:MAX_PICKS]

st.header("🔥 今日過盤推薦 (Top 4)")
if len(picks) == 0:
    st.info("依挑場規則：前 10 場「Pinnacle 真盤」裡，沒有任何一場盤口優勢 > 5%。建議不買、不硬湊。")
else:
    if len(picks) == 1:
        st.success("🎯 今日只有 1 場符合門檻：建議只買單場（或分注單場），不要硬湊串關。")
    else:
        st.success(f"🎯 今日最能買：已依規則挑出 {len(picks)} 場（最多三場）。")

    cols = st.columns(len(picks))
    for idx, item in enumerate(picks):
        g = item["g"]
        with cols[idx]:
            with st.container(border=True):
                st.subheader(f"精選 {idx+1}")
                st.write(f"**{g['label']}**")
                st.success(f"首選：{item['pick_team']}")
                st.caption(f"盤口來源：{item['src']}（候選池=真盤；排序=你輸入的盤口/賠率）")

                st.write(
                    f"過盤機率：**{item['cover_prob']*100:.1f}%** | "
                    f"損益兩平：**{item['implied_prob']*100:.1f}%**"
                )
                st.metric("盤口優勢", f"{item['edge_value']*100:+.1f}%")
                st.write(
                    f"主隊盤口：**{item['home_spread_input']:+.1f}** | "
                    f"主賠：**{item['home_odds']:.2f}** | 客賠：**{item['away_odds']:.2f}**"
                )
                st.write(f"點數優勢：**{item['edge_points']:.1f}** | 期望報酬：**{item['ev']*100:+.1f}%**")

st.divider()

# =========================================================
# 12) 🎯 全部場次與實時計算（保留原 UI；主隊盤口輸入規則；預設帶 Pinnacle）
# =========================================================
st.header("🎯 全部場次與實時計算")

for i in range(0, len(all_games_data), 3):
    cols = st.columns(3)
    for j, g in enumerate(all_games_data[i : i + 3]):
        with cols[j]:
            with st.container(border=True):
                st.subheader(g["label"])
                gid = g["game_id"]

                # 預設值：Pinnacle → 否則 fallback
                sp_default = g["pin_home_sp"]
                oh_default = g["pin_home_od"]
                oa_default = g["pin_away_od"]

                u_sp = st.number_input(
                    "主隊盤口（主讓分填負｜主受讓填正）",
                    min_value=-60.0,
                    max_value=60.0,
                    value=safe_float(st.session_state.get(f"sp_{gid}", sp_default), sp_default),
                    step=0.5,
                    key=f"sp_{gid}",
                )
                u_oh = st.number_input(
                    "主賠（可手動改運彩）",
                    min_value=1.01,
                    max_value=10.0,
                    value=safe_float(st.session_state.get(f"oh_{gid}", oh_default), oh_default),
                    step=0.01,
                    key=f"oh_{gid}",
                )
                u_oa = st.number_input(
                    "客賠（可手動改運彩）",
                    min_value=1.01,
                    max_value=10.0,
                    value=safe_float(st.session_state.get(f"oa_{gid}", oa_default), oa_default),
                    step=0.01,
                    key=f"oa_{gid}",
                )

                # 來源顯示
                manual = (abs(float(u_sp) - sp_default) > 1e-9) or (abs(float(u_oh) - oh_default) > 1e-9) or (abs(float(u_oa) - oa_default) > 1e-9)
                if manual:
                    src = "手動（運彩）✍️"
                elif g["pin_ok"]:
                    src = "Pinnacle ✅"
                else:
                    src = "Fallback ⚠️"

                m = compute_metrics(g, float(u_sp), float(u_oh), float(u_oa))

                st.caption(f"盤口來源：{src}（Top picks 候選池只用 Pinnacle ✅）")
                st.write(f"過盤機率：**{m['cover_prob']*100:.1f}%** | 點數優勢：**{m['edge_points']:.1f}**")
                st.write(f"盤口優勢：**{m['edge_value']*100:+.1f}%** | 期望報酬：**{m['ev']*100:+.1f}%**")

                if g["pin_ok"] and m["edge_value"] > EDGE_THRESHOLD:
                    st.success(f"🔥 符合挑場門檻（真盤候選 + 盤口優勢 > 5%）：{m['pick_team']}")
                else:
                    # 沒真盤或沒過門檻
                    st.info(f"建議：{m['pick_team']}")

# =========================================================
# 13) 🔍 深度查詢（保留原 UI）
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

st.caption(f"（機率曲線參數：prob_scale={PROB_SCALE:.1f}；硬性截斷：{int(PROB_FLOOR*100)}%~{int(PROB_CEIL*100)}%）")
