import streamlit as st
from nba_api.stats.endpoints import (
    scoreboardv2,
    leaguedashplayerstats,
    teamgamelog,
    leaguedashteamstats,
)
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, re, unicodedata, time, math
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# =========================================================
# 1) 核心配置（長期取向：分系 + 風控 + 可回測結構）
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


def get_col(df: pd.DataFrame, col: str, default=0.0):
    if df is None or df.empty or col not in df.columns:
        return default
    return df[col]


# =========================================================
# 2.1) 機率：常態分差近似（避免假 99%）
# =========================================================
MARGIN_SD = 12.0
PROB_FLOOR = 0.12
PROB_CEIL  = 0.88

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def calc_cover_prob(edge_points: float) -> float:
    z = edge_points / MARGIN_SD
    p = norm_cdf(z)
    return max(PROB_FLOOR, min(PROB_CEIL, p))


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
# 5) 團隊效率資料（ORtg/DRtg/PACE/eFG/TOV%/REB%）— cache
#    長期模型最重要的一塊：讓 base_diff 不再只靠 PTS/IMPACT
# =========================================================
@st.cache_data(ttl=3600)
def get_team_stats(season: str = "2025-26") -> pd.DataFrame:
    # Base + Per100Possessions：常見可用欄位包含 ORTG/DRTG/NET_RATING/PACE/eFG_PCT/TOV_PCT/REB_PCT
    ts = fetch_safe_df(
        leaguedashteamstats.LeagueDashTeamStats,
        season=season,
        per_mode_detailed="Per100Possessions",
        measure_type_detailed_defense="Base",
    )
    if ts.empty or "TEAM_ID" not in ts.columns:
        return pd.DataFrame(columns=["TEAM_ID"])

    # 容錯：確保常見欄位存在
    must_cols = ["ORTG", "DRTG", "NET_RATING", "PACE", "EFG_PCT", "TOV_PCT", "REB_PCT"]
    for c in must_cols:
        if c not in ts.columns:
            ts[c] = 0.0

    ts = ts[["TEAM_ID"] + must_cols].copy()
    return ts


# =========================================================
# 6) 傷病報告（ESPN）— cache（含 Q / unknown）
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
                        "IS_Q": bool(is_q),
                        "IS_UNKNOWN": (not is_out and not is_q and not is_ok),
                    }
                )
    except Exception:
        pass

    return pd.DataFrame(inj_list)


# =========================================================
# 7) 隊伍 Context（疲勞分系：b2b + 3in4 + 4in6 + 近10淨勝分 + 波動）— cache
# =========================================================
@st.cache_data(ttl=3600)
def get_team_context(team_ids: list[int], game_date_us: str, season: str = "2025-26") -> dict:
    ctx = {}
    game_day = datetime.strptime(game_date_us, "%m/%d/%Y").date()

    for tid in team_ids:
        log = fetch_safe_df(teamgamelog.TeamGameLog, team_id=tid, season=season)

        # 預設值
        is_b2b = False
        recent_w = 0.5
        margin10 = 0.0
        margin10_sd = 12.0
        in_3in4 = 0
        in_4in6 = 0

        if not log.empty and "GAME_DATE" in log.columns:
            log = log.copy()
            log["GAME_DATE"] = pd.to_datetime(log["GAME_DATE"], errors="coerce").dt.date
            log = log.dropna(subset=["GAME_DATE"])
            # 過去比賽（嚴格在 game_day 之前）
            prior = log[log["GAME_DATE"] < game_day].sort_values("GAME_DATE", ascending=False)

            # b2b
            if not prior.empty:
                last_date = prior.iloc[0]["GAME_DATE"]
                is_b2b = (last_date == (game_day - timedelta(days=1)))

            # 近五勝率
            if "WL" in prior.columns and len(prior.head(5)) > 0:
                recent_w = (prior.head(5)["WL"] == "W").mean()

            # 近10淨勝分（若拿得到 PTS/OPP_PTS）
            if "PTS" in prior.columns and "OPP_PTS" in prior.columns and len(prior.head(10)) > 0:
                last10 = prior.head(10).copy()
                last10["MARGIN"] = pd.to_numeric(last10["PTS"], errors="coerce") - pd.to_numeric(last10["OPP_PTS"], errors="coerce")
                last10 = last10.dropna(subset=["MARGIN"])
                if not last10.empty:
                    margin10 = float(last10["MARGIN"].mean())
                    margin10_sd = float(last10["MARGIN"].std(ddof=0)) if len(last10) >= 3 else 12.0

            # 疲勞：3-in-4 / 4-in-6（用 prior 的日期計算）
            dates = prior.head(10)["GAME_DATE"].tolist()  # 取前10場日期即可
            # 計算 game_day 往回 3/5 天內有幾場（不含 game_day）
            last4_window_start = game_day - timedelta(days=3)
            last6_window_start = game_day - timedelta(days=5)
            cnt_4 = sum((d >= last4_window_start) for d in dates)
            cnt_6 = sum((d >= last6_window_start) for d in dates)
            in_3in4 = 1 if cnt_4 >= 3 else 0
            in_4in6 = 1 if cnt_6 >= 4 else 0

        ctx[tid] = {
            "b2b": bool(is_b2b),
            "recent_w": float(recent_w),
            "margin10": float(margin10),
            "margin10_sd": float(margin10_sd),
            "in_3in4": int(in_3in4),
            "in_4in6": int(in_4in6),
        }
    return ctx


# =========================================================
# 8) UI 初始化（保留原本配置 + 強制更新）
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
        st.rerun()
    with st.popover("💡 判讀指南"):
        st.markdown(
            "**點數優勢**：模型預估分差 vs 主隊盤口（主讓負、主受讓正）差距。\n\n"
            "**盤口優勢**：過盤機率 - 損益兩平機率。\n\n"
            "**期望報酬**：使用保守機率估計的長期期望（不保證獲利）。\n\n"
            "**完整版分系**：效率(ORtg/DRtg/Pace/eFG/TOV/REB) + 近期淨勝分 + 疲勞(3in4/4in6/B2B) + 名單風險。\n"
        )

with st.spinner("⚡ 正在同步美東數據中心..."):
    target_date_us, sb = get_target_scoreboard()
    ps_db = get_player_stats(season="2025-26")
    ts_db = get_team_stats(season="2025-26")
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
    st.warning("⚠️ ESPN 傷病名單目前抓不到（可能改版/阻擋），名單風控會更保守。")

# =========================================================
# 9) 長期風控參數（可回測調整）
# =========================================================
HOME_ADV = 2.2  # 主場加成（可回測校準）

# 分系權重（先給合理起點，後續用回測校準）
W_NET_EFF = 0.45   # season NET_RATING 差
W_OR_DR   = 0.20   # ORtg-DRtg 結構差（補強）
W_MISC    = 0.15   # eFG/TOV/REB 綜合
W_RECENT  = 0.20   # 近10淨勝分修正（保守）

# 疲勞懲罰（點數）
P_B2B = 1.8
P_3IN4 = 1.2
P_4IN6 = 1.6

# 名單不確定性（點數）
TOP_IMPACT_N_FOR_RISK = 6
RISK_PENALTY_Q = 1.8
RISK_PENALTY_UNK = 1.2
MIN_ACTIVE_PLAYERS = 8

# 盤深機率折價（避免大盤假穩）
BIG_SPREAD_PENALTY_K = 0.12  # |spread| 超過 7 分後每多 1 分扣 0.12% 機率（保守）

def spread_depth_prob_penalty(spread: float) -> float:
    d = max(0.0, abs(spread) - 7.0)
    return (BIG_SPREAD_PENALTY_K * d) / 100.0

def team_uncertainty_penalty(team_active: pd.DataFrame, team_inj: pd.DataFrame) -> float:
    if team_active is None or team_active.empty or team_inj is None or team_inj.empty:
        return 0.0
    topN = team_active.head(TOP_IMPACT_N_FOR_RISK)
    if "NORM" not in topN.columns:
        return 0.0
    top_norms = set(topN["NORM"].tolist())
    inj_hit = team_inj[team_inj["NORM"].isin(top_norms)]
    if inj_hit.empty:
        return 0.0

    q_cnt = int(inj_hit.get("IS_Q", pd.Series([False]*len(inj_hit))).sum())
    unk_cnt = int(inj_hit.get("IS_UNKNOWN", pd.Series([False]*len(inj_hit))).sum())
    return q_cnt * RISK_PENALTY_Q + unk_cnt * RISK_PENALTY_UNK


def get_team_row(team_id: int) -> pd.Series:
    if ts_db is None or ts_db.empty:
        return pd.Series(dtype=float)
    r = ts_db[ts_db["TEAM_ID"] == team_id]
    if r.empty:
        return pd.Series(dtype=float)
    return r.iloc[0]


def efficiency_component(home_id: int, away_id: int) -> dict:
    h = get_team_row(home_id)
    a = get_team_row(away_id)

    # 若抓不到就回 0
    h_ortg = float(h.get("ORTG", 0.0))
    h_drtg = float(h.get("DRTG", 0.0))
    h_net  = float(h.get("NET_RATING", 0.0))
    h_pace = float(h.get("PACE", 0.0))
    h_efg  = float(h.get("EFG_PCT", 0.0))
    h_tov  = float(h.get("TOV_PCT", 0.0))
    h_reb  = float(h.get("REB_PCT", 0.0))

    a_ortg = float(a.get("ORTG", 0.0))
    a_drtg = float(a.get("DRTG", 0.0))
    a_net  = float(a.get("NET_RATING", 0.0))
    a_pace = float(a.get("PACE", 0.0))
    a_efg  = float(a.get("EFG_PCT", 0.0))
    a_tov  = float(a.get("TOV_PCT", 0.0))
    a_reb  = float(a.get("REB_PCT", 0.0))

    # 分系差（主-客）
    net_diff = h_net - a_net
    ordr_diff = (h_ortg - h_drtg) - (a_ortg - a_drtg)

    # misc：eFG 越高越好；TOV% 越低越好；REB% 越高越好
    misc_h = (h_efg * 100) - (h_tov) + (h_reb * 100)
    misc_a = (a_efg * 100) - (a_tov) + (a_reb * 100)
    misc_diff = misc_h - misc_a

    # pace mismatch：不是直接好壞，但 mismatch 越大波動越大 → 我們在深度查詢呈現，不直接加分
    pace_diff = h_pace - a_pace

    return {
        "net_diff": net_diff,
        "ordr_diff": ordr_diff,
        "misc_diff": misc_diff,
        "pace_diff": pace_diff,
        "h_stats": {"ORTG": h_ortg, "DRTG": h_drtg, "NET": h_net, "PACE": h_pace, "eFG": h_efg, "TOV": h_tov, "REB": h_reb},
        "a_stats": {"ORTG": a_ortg, "DRTG": a_drtg, "NET": a_net, "PACE": a_pace, "eFG": a_efg, "TOV": a_tov, "REB": a_reb},
    }


# =========================================================
# 10) 主計算：建立每場 pkg + base_diff（效率分系 + 近期 + 疲勞 + 名單風險）
# =========================================================
all_games_data = []

for _, row in sb_filtered.iterrows():
    h_id, a_id = int(row["HOME_TEAM_ID"]), int(row["VISITOR_TEAM_ID"])
    h_abbr, a_abbr = ID_MAP.get(h_id, str(h_id)), ID_MAP.get(a_id, str(a_id))

    def build_pkg(tid: int, abbr: str):
        ctx = ctx_db.get(tid, {"b2b": False, "recent_w": 0.5, "margin10": 0.0, "margin10_sd": 12.0, "in_3in4": 0, "in_4in6": 0})

        t_inj = inj_db[inj_db["球隊"] == abbr] if not inj_db.empty else pd.DataFrame()
        out_list = t_inj[t_inj["IS_OUT"]]["NORM"].tolist() if (not t_inj.empty and "IS_OUT" in t_inj.columns) else []

        if not ps_db.empty and "TEAM_ID" in ps_db.columns and "NORM" in ps_db.columns:
            active = (
                ps_db[(ps_db["TEAM_ID"] == tid) & (~ps_db["NORM"].isin(out_list))]
                .sort_values("IMPACT", ascending=False)
                .copy()
            )
        else:
            active = pd.DataFrame()

        return {
            "df": active,
            "inj": t_inj,
            "active_n": int(len(active)) if active is not None else 0,
            "b2b": bool(ctx["b2b"]),
            "recent_w": float(ctx["recent_w"]),
            "margin10": float(ctx["margin10"]),
            "margin10_sd": float(ctx["margin10_sd"]),
            "in_3in4": int(ctx["in_3in4"]),
            "in_4in6": int(ctx["in_4in6"]),
        }

    h_p, a_p = build_pkg(h_id, h_abbr), build_pkg(a_id, a_abbr)

    # 1) 效率分系
    eff = efficiency_component(h_id, a_id)
    net_eff_points = eff["net_diff"] * 0.55          # NET_RATING 每 1 點 ≈ 0.55 分（起始值，回測校準）
    ordr_points    = eff["ordr_diff"] * 0.20         # 結構補強
    misc_points    = eff["misc_diff"] * 0.03         # misc 是百分比尺度，縮小

    # 2) 近期分系（近10淨勝分差距，保守）
    recent_points = (h_p["margin10"] - a_p["margin10"]) * 0.35

    # 3) 主場
    home_points = HOME_ADV

    # 4) 疲勞懲罰（主-客）
    fatigue_h = (P_B2B if h_p["b2b"] else 0) + (P_3IN4 if h_p["in_3in4"] else 0) + (P_4IN6 if h_p["in_4in6"] else 0)
    fatigue_a = (P_B2B if a_p["b2b"] else 0) + (P_3IN4 if a_p["in_3in4"] else 0) + (P_4IN6 if a_p["in_4in6"] else 0)
    fatigue_points = -fatigue_h + fatigue_a

    # 5) 名單不確定性折價
    h_pen = team_uncertainty_penalty(h_p["df"], h_p["inj"])
    a_pen = team_uncertainty_penalty(a_p["df"], a_p["inj"])

    # 6) 組合 base_diff（主隊預估分差）
    base_diff_raw = (
        W_NET_EFF * net_eff_points
        + W_OR_DR  * ordr_points
        + W_MISC   * misc_points
        + W_RECENT * recent_points
        + home_points
        + fatigue_points
    )
    base_diff = base_diff_raw - h_pen + a_pen

    # 風險旗標
    risk_flags = []
    if h_p["active_n"] < MIN_ACTIVE_PLAYERS:
        risk_flags.append("⚠️ 主隊可用人數不足")
    if a_p["active_n"] < MIN_ACTIVE_PLAYERS:
        risk_flags.append("⚠️ 客隊可用人數不足")
    if h_pen > 0:
        risk_flags.append("⚠️ 主隊名單不確定")
    if a_pen > 0:
        risk_flags.append("⚠️ 客隊名單不確定")

    game_id = f"{a_abbr}_{h_abbr}_{target_date_us.replace('/','')}"

    all_games_data.append(
        {
            "game_id": game_id,
            "label": f"{TEAM_NAME_CH.get(a_abbr, a_abbr)}(客) @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}(主)",
            "base_diff": float(base_diff),
            "base_diff_raw": float(base_diff_raw),
            "h_pkg": h_p,
            "a_pkg": a_p,
            "h_cn": TEAM_NAME_CH.get(h_abbr, h_abbr),
            "a_cn": TEAM_NAME_CH.get(a_abbr, a_abbr),
            "risk_flags": risk_flags,
            "eff": eff,
        }
    )


# =========================================================
# 11) 挑場規則（長期 +EV）
# =========================================================
EDGE_THRESHOLD = 0.08       # 長期門檻：盤口優勢 > 8%
MAX_PICKS = 3
MAX_GAMES_FOR_PICK = 10

def get_market_inputs_for_game(g):
    gid = g["game_id"]
    sp = st.session_state.get(f"sp_{gid}", 0.0)
    oh = st.session_state.get(f"oh_{gid}", 1.90)
    oa = st.session_state.get(f"oa_{gid}", 1.90)
    return float(sp), float(oh), float(oa)

def has_user_market_input(g):
    gid = g["game_id"]
    sp = float(st.session_state.get(f"sp_{gid}", 0.0))
    oh = float(st.session_state.get(f"oh_{gid}", 1.90))
    oa = float(st.session_state.get(f"oa_{gid}", 1.90))
    return (abs(sp) > 0.0) or (abs(oh - 1.90) > 1e-9) or (abs(oa - 1.90) > 1e-9)


# =========================================================
# 12) 🔥 今日最能買（至多三場）
# =========================================================
st.header("🔥 今日最能買（至多三場）")

pick_pool = []
for g in all_games_data[:MAX_GAMES_FOR_PICK]:
    # 名單不足：直接不納入精選池（避免被打爆）
    if any("可用人數不足" in f for f in g["risk_flags"]):
        continue

    # 長期模式：沒盤口/賠率就不挑（避免假 EV）
    if not has_user_market_input(g):
        continue

    u_sp, u_oh, u_oa = get_market_inputs_for_game(g)

    # f_edge：主隊在盤口下的優勢（>0 主隊更可能過盤；<0 客隊）
    f_edge = g["base_diff"] + u_sp

    cover_prob = calc_cover_prob(f_edge)
    cover_prob = max(PROB_FLOOR, min(PROB_CEIL, cover_prob - spread_depth_prob_penalty(u_sp)))

    pick_side = g["h_cn"] if f_edge > 0 else g["a_cn"]
    odds = u_oh if f_edge > 0 else u_oa
    implied_prob = 1.0 / odds if odds and odds > 0 else 1.0

    edge_value = cover_prob - implied_prob
    ev = (cover_prob * odds) - 1

    pick_pool.append({
        "g": g,
        "pick_side": pick_side,
        "cover_prob": cover_prob,
        "implied_prob": implied_prob,
        "edge_value": edge_value,
        "edge_points": abs(f_edge),
        "odds": odds,
        "home_spread_input": u_sp,
        "ev": ev
    })

qualified = [x for x in pick_pool if x["edge_value"] > EDGE_THRESHOLD]
qualified.sort(key=lambda x: (x["cover_prob"], x["edge_value"]), reverse=True)
picks = qualified[:MAX_PICKS]

if len(picks) == 0:
    st.info("長期模式：請先輸入盤口/賠率；且需『盤口優勢 > 8%』才列入精選。今天目前沒有符合條件的場次。")
else:
    if len(picks) == 1:
        st.success("🎯 今日只有 1 場達到門檻：建議單場，不要硬湊串關。")
    else:
        st.success(f"🎯 今日精選 {len(picks)} 場（最多三場）：依盤口優勢/過盤機率排序。")

    cols = st.columns(len(picks))
    for idx, item in enumerate(picks):
        g = item["g"]
        with cols[idx]:
            with st.container(border=True):
                st.subheader(f"精選 {idx+1}")
                st.write(f"**{g['label']}**")
                st.success(f"首選：{item['pick_side']}")

                if g["risk_flags"]:
                    st.warning("｜".join(g["risk_flags"]))

                st.write(
                    f"過盤機率：**{item['cover_prob']*100:.1f}%** | "
                    f"損益兩平：**{item['implied_prob']*100:.1f}%**"
                )
                st.metric("盤口優勢", f"{item['edge_value']*100:+.1f}%")
                st.write(f"主隊盤口：**{item['home_spread_input']}** | 賠率：**{item['odds']:.2f}**")
                st.write(f"點數優勢：**{item['edge_points']:.1f}**")
                st.write(f"期望報酬：**{item['ev']*100:+.1f}%**")

st.divider()


# =========================================================
# 13) 🎯 全部場次與實時計算（保留原 UI；主隊盤口輸入規則）
# =========================================================
st.header("🎯 全部場次與實時計算")

for i in range(0, len(all_games_data), 3):
    cols = st.columns(3)
    for j, g in enumerate(all_games_data[i : i + 3]):
        with cols[j]:
            with st.container(border=True):
                st.subheader(g["label"])

                gid = g["game_id"]

                u_sp = st.number_input(
                    "主隊盤口（主讓分填負｜主受讓填正）",
                    min_value=-60.0,
                    max_value=60.0,
                    value=float(st.session_state.get(f"sp_{gid}", 0.0)),
                    step=0.5,
                    key=f"sp_{gid}",
                )
                u_oh = st.number_input(
                    "主賠",
                    min_value=1.01,
                    max_value=5.0,
                    value=float(st.session_state.get(f"oh_{gid}", 1.90)),
                    step=0.01,
                    key=f"oh_{gid}",
                )
                u_oa = st.number_input(
                    "客賠",
                    min_value=1.01,
                    max_value=5.0,
                    value=float(st.session_state.get(f"oa_{gid}", 1.90)),
                    step=0.01,
                    key=f"oa_{gid}",
                )

                if g["risk_flags"]:
                    st.warning("｜".join(g["risk_flags"]))

                f_edge = g["base_diff"] + u_sp
                cover_prob = calc_cover_prob(f_edge)
                cover_prob = max(PROB_FLOOR, min(PROB_CEIL, cover_prob - spread_depth_prob_penalty(u_sp)))

                rec = g["h_cn"] if f_edge > 0 else g["a_cn"]
                odds = u_oh if f_edge > 0 else u_oa

                implied_prob = 1.0 / odds if odds and odds > 0 else 1.0
                edge_value = cover_prob - implied_prob
                ev = (cover_prob * odds) - 1

                st.write(f"過盤機率：**{cover_prob*100:.1f}%** | 點數優勢：**{abs(f_edge):.1f}**")
                st.write(f"盤口優勢：**{edge_value*100:+.1f}%** | 期望報酬：**{ev*100:+.1f}%**")

                if edge_value > EDGE_THRESHOLD and not any("可用人數不足" in f for f in g["risk_flags"]):
                    st.success(f"🔥 符合長期門檻（盤口優勢 > {EDGE_THRESHOLD*100:.0f}%）：{rec}")
                else:
                    st.info(f"建議：{rec}")


# =========================================================
# 14) 🔍 深度查詢（加你要的「更多分系」：效率/近期/疲勞/波動/名單風險）
# =========================================================
st.divider()
st.header("🔍 深度數據查詢（完整版分系）")

sel = st.selectbox("請選擇場次", [g["label"] for g in all_games_data])
if sel:
    curr = next(g for g in all_games_data if g["label"] == sel)

    st.write(
        f"📊 **戰前速報**："
        f"{'🚨 客隊背靠背' if curr['a_pkg']['b2b'] else '✅ 客隊非背靠背'} | "
        f"{'🚨 主隊背靠背' if curr['h_pkg']['b2b'] else '✅ 主隊非背靠背'}"
    )
    if curr["risk_flags"]:
        st.warning("｜".join(curr["risk_flags"]))

    # 1) 分系拆解（模型用的）
    eff = curr.get("eff", {})
    st.subheader("① 效率分系（Season Per100）")
    cA, cB, cC = st.columns(3)
    with cA:
        st.write(f"主隊 NET：**{eff.get('h_stats', {}).get('NET', 0):.1f}** | 客隊 NET：**{eff.get('a_stats', {}).get('NET', 0):.1f}**")
        st.write(f"NET 差（主-客）：**{eff.get('net_diff', 0):+.1f}**")
    with cB:
        st.write(f"主 ORtg/DRtg：**{eff.get('h_stats', {}).get('ORTG', 0):.1f}/{eff.get('h_stats', {}).get('DRTG', 0):.1f}**")
        st.write(f"客 ORtg/DRtg：**{eff.get('a_stats', {}).get('ORTG', 0):.1f}/{eff.get('a_stats', {}).get('DRTG', 0):.1f}**")
        st.write(f"結構差（OR-DR）：**{eff.get('ordr_diff', 0):+.1f}**")
    with cC:
        st.write(f"Pace（主/客）：**{eff.get('h_stats', {}).get('PACE', 0):.1f} / {eff.get('a_stats', {}).get('PACE', 0):.1f}**")
        st.write(f"Pace 差（主-客）：**{eff.get('pace_diff', 0):+.1f}**（差越大波動越大）")

    st.subheader("② 近期/疲勞/波動分系（最近比賽）")
    r1, r2, r3 = st.columns(3)
    with r1:
        st.write(f"主隊近10淨勝分：**{curr['h_pkg']['margin10']:+.1f}** | 波動SD：**{curr['h_pkg']['margin10_sd']:.1f}**")
        st.write(f"客隊近10淨勝分：**{curr['a_pkg']['margin10']:+.1f}** | 波動SD：**{curr['a_pkg']['margin10_sd']:.1f}**")
    with r2:
        st.write(f"主隊 3-in-4：**{curr['h_pkg']['in_3in4']}** | 4-in-6：**{curr['h_pkg']['in_4in6']}**")
        st.write(f"客隊 3-in-4：**{curr['a_pkg']['in_3in4']}** | 4-in-6：**{curr['a_pkg']['in_4in6']}**")
    with r3:
        st.write(f"主隊近五勝率：**{curr['h_pkg']['recent_w']*100:.0f}%**")
        st.write(f"客隊近五勝率：**{curr['a_pkg']['recent_w']*100:.0f}%**")

    st.subheader("③ 名單/球員分系（排除確定缺陣後）")
    c1, c2 = st.columns(2)
    for col, pkg, side, team_name in [
        (c1, curr["h_pkg"], "(主)", curr["h_cn"]),
        (c2, curr["a_pkg"], "(客)", curr["a_cn"]),
    ]:
        with col:
            st.write(f"**{team_name} {side}**")
            st.write(f"可用球員數：**{pkg.get('active_n', 0)}**（低於 {MIN_ACTIVE_PLAYERS} 會直接不列精選）")

            if pkg["df"] is not None and not pkg["df"].empty:
                show_cols = [c for c in ["PLAYER_NAME", "MIN", "PTS", "IMPACT"] if c in pkg["df"].columns]
                st.dataframe(pkg["df"][show_cols].head(12), hide_index=True)
            else:
                st.write("（球員資料不足或 API 暫時不可用）")

            if pkg["inj"] is not None and not pkg["inj"].empty:
                st.dataframe(pkg["inj"][["球員", "狀態", "原因"]], hide_index=True)
            else:
                st.write("✅ 無傷病報告")
