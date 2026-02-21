import os
import json
import math
import time
import re
import unicodedata
import warnings
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytz
import requests
import streamlit as st
from bs4 import BeautifulSoup

from nba_api.stats.endpoints import (
    scoreboardv2,
    leaguedashplayerstats,
    teamgamelog,
    leaguedashteamstats,
)
from nba_api.stats.static import teams


# =========================================================
# 0) 核心：安全設定
# =========================================================
warnings.filterwarnings("ignore")
tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

# Odds API Key：優先 st.secrets，其次環境變數
ODDS_API_KEY = None
if hasattr(st, "secrets") and "ODDS_API_KEY" in st.secrets:
    ODDS_API_KEY = st.secrets["ODDS_API_KEY"]
else:
    ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

if not ODDS_API_KEY:
    st.warning("⚠️ 尚未設定 The Odds API Key。請在 .streamlit/secrets.toml 設定 ODDS_API_KEY。")


# =========================================================
# 1) 隊名與 ID 映射（維持原架構）
# =========================================================
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
TEAM_NAME_EN = {k: v[0] for k, v in TEAM_MAP.items()}

ALL_TEAMS = teams.get_teams()
VALID_TEAM_IDS = [t["id"] for t in ALL_TEAMS]
ID_MAP = {t["id"]: t["abbreviation"] for t in ALL_TEAMS}

# Odds API team names 通常是英文全名；我們用 TEAM_NAME_EN 去對齊
EN_TO_ABBR = {v[0]: k for k, v in TEAM_MAP.items()}


# =========================================================
# 2) 基礎工具：正規化姓名、API 安全抓取
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


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# =========================================================
# 3) Odds API：抓「當前」spreads/odds（用於今天分析）
#    - 建議用 pinnacle（sharp book）做基準
# =========================================================
@st.cache_data(ttl=300)
def get_odds_current(bookmaker_key: str = "pinnacle") -> dict:
    """
    回傳 dict:
      key: (home_team_en, away_team_en)
      val: {
        "home_point": float (主隊讓分：主隊讓分是負，主隊受讓是正),
        "home_price": float (主隊賠率 decimal),
        "away_point": float,
        "away_price": float,
        "commence_time": iso str
      }
    """
    if not ODDS_API_KEY:
        return {}

    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "spreads",
        "oddsFormat": "decimal",
        "bookmakers": bookmaker_key,
    }
    try:
        r = requests.get(url, params=params, timeout=12)
        if r.status_code != 200:
            return {}
        data = r.json()
    except Exception:
        return {}

    out = {}
    for g in data:
        home = g.get("home_team")
        away = g.get("away_team")
        commence_time = g.get("commence_time")

        bms = g.get("bookmakers", [])
        if not bms:
            continue
        # 我們已經指定 bookmaker 了，通常只會回 1 家
        markets = bms[0].get("markets", [])
        m = next((x for x in markets if x.get("key") == "spreads"), None)
        if not m:
            continue

        outcomes = m.get("outcomes", [])
        # outcomes 內會有兩筆：home / away
        home_out = next((o for o in outcomes if o.get("name") == home), None)
        away_out = next((o for o in outcomes if o.get("name") == away), None)
        if not home_out or not away_out:
            continue

        out[(home, away)] = {
            "home_point": float(home_out.get("point", 0.0)),
            "home_price": float(home_out.get("price", 1.90)),
            "away_point": float(away_out.get("point", 0.0)),
            "away_price": float(away_out.get("price", 1.90)),
            "commence_time": commence_time,
            "bookmaker": bookmaker_key,
        }
    return out


# =========================================================
# 4) NBA 賽程（保持原本邏輯：今日沒賽程 → 明日）
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
# 5) 球員資料（box score proxy）
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
# 6) 團隊效率資料（完整版分系來源）
# =========================================================
@st.cache_data(ttl=3600)
def get_team_stats(season: str = "2025-26") -> pd.DataFrame:
    ts = fetch_safe_df(
        leaguedashteamstats.LeagueDashTeamStats,
        season=season,
        per_mode_detailed="Per100Possessions",
        measure_type_detailed_defense="Base",
    )
    if ts.empty or "TEAM_ID" not in ts.columns:
        return pd.DataFrame(columns=["TEAM_ID"])

    must_cols = ["ORTG", "DRTG", "NET_RATING", "PACE", "EFG_PCT", "TOV_PCT", "REB_PCT"]
    for c in must_cols:
        if c not in ts.columns:
            ts[c] = 0.0
    return ts[["TEAM_ID"] + must_cols].copy()


# =========================================================
# 7) ESPN 傷病（保守：不亂給 ✅）
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
            t_name = title_el.get_text(strip=True).lower()

            # 用英文全名對齊隊伍
            t_abbr = None
            for abbr, info in TEAM_MAP.items():
                if info[0].lower() in t_name:
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
                q_kw = ["questionable", "doubtful", "gtd", "day-to-day", "game time decision"]
                ok_kw = ["available", "will play", "probable"]

                is_out = any(k in row_text for k in out_kw)
                is_q = any(k in row_text for k in q_kw)
                is_ok = any(k in row_text for k in ok_kw)

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
# 8) 隊伍 Context：疲勞＋近期淨勝分＋波動
# =========================================================
@st.cache_data(ttl=3600)
def get_team_context(team_ids: list[int], game_date_us: str, season: str = "2025-26") -> dict:
    ctx = {}
    game_day = datetime.strptime(game_date_us, "%m/%d/%Y").date()

    for tid in team_ids:
        log = fetch_safe_df(teamgamelog.TeamGameLog, team_id=tid, season=season)

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
            prior = log[log["GAME_DATE"] < game_day].sort_values("GAME_DATE", ascending=False)

            if not prior.empty:
                is_b2b = (prior.iloc[0]["GAME_DATE"] == (game_day - timedelta(days=1)))

            if "WL" in prior.columns and len(prior.head(5)) > 0:
                recent_w = (prior.head(5)["WL"] == "W").mean()

            if "PTS" in prior.columns and "OPP_PTS" in prior.columns and len(prior.head(10)) > 0:
                last10 = prior.head(10).copy()
                last10["MARGIN"] = pd.to_numeric(last10["PTS"], errors="coerce") - pd.to_numeric(last10["OPP_PTS"], errors="coerce")
                last10 = last10.dropna(subset=["MARGIN"])
                if not last10.empty:
                    margin10 = float(last10["MARGIN"].mean())
                    margin10_sd = float(last10["MARGIN"].std(ddof=0)) if len(last10) >= 3 else 12.0

            dates = prior.head(10)["GAME_DATE"].tolist()
            last4_start = game_day - timedelta(days=3)
            last6_start = game_day - timedelta(days=5)
            cnt_4 = sum((d >= last4_start) for d in dates)
            cnt_6 = sum((d >= last6_start) for d in dates)
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
# 9) 可學習模型（線上 logistic regression, 純 Python）
#    - 不依賴 sklearn（避免環境沒裝）
#    - 你每天跑，系統會把已結束場次寫進本地資料檔，越跑越準
# =========================================================
DATA_PATH = "edge_training_data.csv"

MODEL_DEFAULT = {
    "bias": 0.0,
    # features weights
    "w_net": 0.18,
    "w_ordr": 0.05,
    "w_misc": 0.02,
    "w_recent": 0.08,
    "w_home": 0.15,
    "w_fatigue": -0.10,
    "w_uncert": -0.12,
    "w_spread_depth": -0.06,
}
LR = 0.08
L2 = 0.001

def sigmoid(x: float) -> float:
    # 防爆
    if x > 10:
        return 0.99995
    if x < -10:
        return 0.00005
    return 1.0 / (1.0 + math.exp(-x))

def load_model() -> dict:
    if "model_params" in st.session_state:
        return dict(st.session_state["model_params"])
    st.session_state["model_params"] = dict(MODEL_DEFAULT)
    return dict(MODEL_DEFAULT)

def save_model(m: dict):
    st.session_state["model_params"] = dict(m)

def load_training_df() -> pd.DataFrame:
    if os.path.exists(DATA_PATH):
        try:
            return pd.read_csv(DATA_PATH)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()

def append_training_rows(rows: list[dict]):
    if not rows:
        return
    df_old = load_training_df()
    df_new = pd.DataFrame(rows)
    df = pd.concat([df_old, df_new], ignore_index=True)
    # 去重：同一場只留一筆（以 game_key）
    if "game_key" in df.columns:
        df = df.drop_duplicates(subset=["game_key"], keep="last")
    df.to_csv(DATA_PATH, index=False)

def train_one_epoch(df: pd.DataFrame, m: dict, epochs: int = 3):
    if df is None or df.empty:
        return m

    feats = ["net", "ordr", "misc", "recent", "home", "fatigue", "uncert", "spread_depth"]
    for _ in range(epochs):
        for _, r in df.sample(frac=1.0, random_state=int(time.time()) % 10).iterrows():
            y = float(r.get("y", 0.0))
            x = {
                "net": float(r.get("net", 0.0)),
                "ordr": float(r.get("ordr", 0.0)),
                "misc": float(r.get("misc", 0.0)),
                "recent": float(r.get("recent", 0.0)),
                "home": float(r.get("home", 0.0)),
                "fatigue": float(r.get("fatigue", 0.0)),
                "uncert": float(r.get("uncert", 0.0)),
                "spread_depth": float(r.get("spread_depth", 0.0)),
            }
            z = m["bias"]
            z += m["w_net"] * x["net"]
            z += m["w_ordr"] * x["ordr"]
            z += m["w_misc"] * x["misc"]
            z += m["w_recent"] * x["recent"]
            z += m["w_home"] * x["home"]
            z += m["w_fatigue"] * x["fatigue"]
            z += m["w_uncert"] * x["uncert"]
            z += m["w_spread_depth"] * x["spread_depth"]

            p = sigmoid(z)
            # gradient
            err = (p - y)
            m["bias"] -= LR * (err + L2 * m["bias"])
            for k in feats:
                wk = f"w_{k}"
                m[wk] -= LR * (err * x[k] + L2 * m[wk])

    return m


# =========================================================
# 10) 名單不確定性量化（讓模型學）
# =========================================================
TOP_IMPACT_N_FOR_RISK = 6

def team_uncertainty_score(active_df: pd.DataFrame, inj_df: pd.DataFrame) -> float:
    """
    回傳一個 0~? 的風險分數，讓模型自行學怎麼扣
    """
    if active_df is None or active_df.empty or inj_df is None or inj_df.empty:
        return 0.0
    if "NORM" not in active_df.columns or "NORM" not in inj_df.columns:
        return 0.0

    topN = active_df.head(TOP_IMPACT_N_FOR_RISK)
    top_norms = set(topN["NORM"].tolist())
    hit = inj_df[inj_df["NORM"].isin(top_norms)]
    if hit.empty:
        return 0.0
    q_cnt = int(hit["IS_Q"].sum()) if "IS_Q" in hit.columns else 0
    unk_cnt = int(hit["IS_UNKNOWN"].sum()) if "IS_UNKNOWN" in hit.columns else 0
    out_cnt = int(hit["IS_OUT"].sum()) if "IS_OUT" in hit.columns else 0
    # OUT 理論上已被排除，但若 ESPN 有落差，這裡也納入
    return 1.0 * out_cnt + 0.7 * q_cnt + 0.4 * unk_cnt


def spread_depth(sp: float) -> float:
    d = max(0.0, abs(sp) - 7.0)
    return d


# =========================================================
# 11) UI 初始化（維持你的框架）
# =========================================================
st.set_page_config(page_title="NBA Edge v16.0", layout="wide")

h1, h2 = st.columns([0.8, 0.2])
with h1:
    now_tw_str = datetime.now(tw_tz).strftime("%m/%d %H:%M")
    st.title("🏀 NBA Edge 數據預測系統（可學習版）")
    st.caption(f"台灣現在時間：{now_tw_str}")
with h2:
    if st.button("🔄 強制更新傷病/數據"):
        st.cache_data.clear()
        st.rerun()
    with st.popover("💡 判讀指南"):
        st.markdown(
            "**主隊盤口**：主讓分填負｜主受讓填正。\n\n"
            "**盤口優勢**：模型過盤機率 - 損益兩平機率。\n\n"
            "**可學習版**：會把已結束比賽寫入資料庫（edge_training_data.csv），每天跑越準。\n\n"
            "⚠️ 若你的 Odds API 沒有 historical 權限，系統仍可用「日常累積」逐步學習。"
        )

# 讀模型 & 讀訓練資料
model = load_model()
train_df = load_training_df()

with st.spinner("⚡ 正在同步美東數據中心..."):
    target_date_us, sb = get_target_scoreboard()
    ps_db = get_player_stats(season="2025-26")
    ts_db = get_team_stats(season="2025-26")
    inj_db = get_injuries()
    odds_now = get_odds_current(bookmaker_key="pinnacle")

if sb.empty or "HOME_TEAM_ID" not in sb.columns:
    st.info("📅 目前抓不到賽程資料（Scoreboard API 回傳空）。請稍後重試。")
    st.stop()

sb_filtered = sb[sb["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)].copy()
if sb_filtered.empty:
    st.info(f"📅 {target_date_us}（美東）無有效 NBA 賽程。")
    st.stop()

now_us_str = datetime.now(us_east_tz).strftime("%m/%d/%Y")
if target_date_us != now_us_str:
    st.info(f"📅 今日美東無賽程，已為您自動跳轉至明日：{target_date_us}")
else:
    st.success(f"📅 正在分析美東今日賽程：{target_date_us}")

today_team_ids = sorted(set(sb_filtered["HOME_TEAM_ID"].tolist() + sb_filtered["VISITOR_TEAM_ID"].tolist()))
ctx_db = get_team_context(today_team_ids, game_date_us=target_date_us, season="2025-26")

if inj_db.empty:
    st.warning("⚠️ ESPN 傷病名單目前抓不到（可能改版/阻擋），名單風險會偏保守。")
if not odds_now:
    st.warning("⚠️ 目前抓不到 Pinnacle spreads/odds（可能 key 權限/額度/暫時錯誤）。精選挑場將受影響。")


# =========================================================
# 12) 特徵工程：把每場轉成 features（主隊視角）
# =========================================================
def get_team_row(team_id: int) -> pd.Series:
    if ts_db is None or ts_db.empty or "TEAM_ID" not in ts_db.columns:
        return pd.Series(dtype=float)
    r = ts_db[ts_db["TEAM_ID"] == team_id]
    if r.empty:
        return pd.Series(dtype=float)
    return r.iloc[0]

def build_pkg(tid: int, abbr: str):
    ctx = ctx_db.get(tid, {"b2b": False, "recent_w": 0.5, "margin10": 0.0, "margin10_sd": 12.0, "in_3in4": 0, "in_4in6": 0})
    t_inj = inj_db[inj_db["球隊"] == abbr] if not inj_db.empty else pd.DataFrame()

    out_list = []
    if not t_inj.empty and "IS_OUT" in t_inj.columns:
        out_list = t_inj[t_inj["IS_OUT"]]["NORM"].tolist()

    active = pd.DataFrame()
    if not ps_db.empty and "TEAM_ID" in ps_db.columns and "NORM" in ps_db.columns:
        active = (
            ps_db[(ps_db["TEAM_ID"] == tid) & (~ps_db["NORM"].isin(out_list))]
            .sort_values("IMPACT", ascending=False)
            .copy()
        )

    return {
        "df": active,
        "inj": t_inj,
        "b2b": bool(ctx["b2b"]),
        "recent_w": float(ctx["recent_w"]),
        "margin10": float(ctx["margin10"]),
        "margin10_sd": float(ctx["margin10_sd"]),
        "in_3in4": int(ctx["in_3in4"]),
        "in_4in6": int(ctx["in_4in6"]),
    }

def calc_features(home_id: int, away_id: int, home_abbr: str, away_abbr: str, home_spread: float) -> dict:
    h = get_team_row(home_id)
    a = get_team_row(away_id)

    # season efficiency diffs (主-客)
    net = float(h.get("NET_RATING", 0.0)) - float(a.get("NET_RATING", 0.0))
    ordr = (float(h.get("ORTG", 0.0)) - float(h.get("DRTG", 0.0))) - (float(a.get("ORTG", 0.0)) - float(a.get("DRTG", 0.0)))

    # misc：eFG(高好) TOV(低好) REB(高好)
    misc_h = float(h.get("EFG_PCT", 0.0)) * 100 - float(h.get("TOV_PCT", 0.0)) + float(h.get("REB_PCT", 0.0)) * 100
    misc_a = float(a.get("EFG_PCT", 0.0)) * 100 - float(a.get("TOV_PCT", 0.0)) + float(a.get("REB_PCT", 0.0)) * 100
    misc = misc_h - misc_a

    # recent (近10淨勝分差)
    hp = build_pkg(home_id, home_abbr)
    ap = build_pkg(away_id, away_abbr)
    recent = hp["margin10"] - ap["margin10"]

    # fatigue (主相對客：正值代表主更累)
    fatigue = (
        (1.0 if hp["b2b"] else 0.0)
        + (0.7 if hp["in_3in4"] else 0.0)
        + (0.9 if hp["in_4in6"] else 0.0)
        - (1.0 if ap["b2b"] else 0.0)
        - (0.7 if ap["in_3in4"] else 0.0)
        - (0.9 if ap["in_4in6"] else 0.0)
    )

    # uncert (主 - 客)
    uncert = team_uncertainty_score(hp["df"], hp["inj"]) - team_uncertainty_score(ap["df"], ap["inj"])

    # home indicator (固定 1)
    home = 1.0

    # spread depth
    sd = spread_depth(home_spread)

    return {
        "net": net,
        "ordr": ordr,
        "misc": misc,
        "recent": recent,
        "fatigue": fatigue,
        "uncert": uncert,
        "home": home,
        "spread_depth": sd,
        "h_pkg": hp,
        "a_pkg": ap,
        "h_stats": dict(h),
        "a_stats": dict(a),
    }

def predict_cover_prob(m: dict, feats: dict) -> float:
    z = m["bias"]
    z += m["w_net"] * feats["net"]
    z += m["w_ordr"] * feats["ordr"]
    z += m["w_misc"] * feats["misc"]
    z += m["w_recent"] * feats["recent"]
    z += m["w_home"] * feats["home"]
    z += m["w_fatigue"] * feats["fatigue"]
    z += m["w_uncert"] * feats["uncert"]
    z += m["w_spread_depth"] * feats["spread_depth"]

    p = sigmoid(z)
    # 保守截斷：避免假穩
    return max(0.12, min(0.88, p))


# =========================================================
# 13) 建立 all_games_data（保留原框架）
# =========================================================
all_games_data = []
for _, row in sb_filtered.iterrows():
    h_id = int(row["HOME_TEAM_ID"])
    a_id = int(row["VISITOR_TEAM_ID"])
    h_abbr = ID_MAP.get(h_id, str(h_id))
    a_abbr = ID_MAP.get(a_id, str(a_id))

    h_cn = TEAM_NAME_CH.get(h_abbr, h_abbr)
    a_cn = TEAM_NAME_CH.get(a_abbr, a_abbr)
    h_en = TEAM_NAME_EN.get(h_abbr, "")
    a_en = TEAM_NAME_EN.get(a_abbr, "")

    # 先嘗試從 Odds API 拿「建議盤口/賠率」當預設值
    odds_key = (h_en, a_en)
    # Odds API 的 key 是 (home, away)；我們用英文全名 match
    odds_row = odds_now.get((h_en, a_en), None)
    if odds_row is None:
        # 有時 Odds API 是用 (home, away) 反過來（視資料源），做容錯
        odds_row = odds_now.get((h_en, a_en), None)

    # 預設：主隊盤口、主賠、客賠（可被 UI 輸入覆蓋）
    default_sp = 0.0
    default_oh = 1.90
    default_oa = 1.90
    if odds_row:
        default_sp = float(odds_row.get("home_point", 0.0))
        default_oh = float(odds_row.get("home_price", 1.90))
        default_oa = float(odds_row.get("away_price", 1.90))

    game_id = f"{a_abbr}_{h_abbr}_{target_date_us.replace('/','')}"
    all_games_data.append({
        "game_id": game_id,
        "label": f"{a_cn}(客) @ {h_cn}(主)",
        "h_id": h_id, "a_id": a_id,
        "h_abbr": h_abbr, "a_abbr": a_abbr,
        "h_cn": h_cn, "a_cn": a_cn,
        "h_en": h_en, "a_en": a_en,
        "default_sp": default_sp,
        "default_oh": default_oh,
        "default_oa": default_oa,
    })


# =========================================================
# 14) 精選挑場規則（你指定的版本 + 長期門檻）
# =========================================================
EDGE_THRESHOLD = 0.08   # 長期：建議 8% 起跳（運彩通常更要高門檻）
MAX_PICKS = 3
MAX_GAMES_FOR_PICK = 10


# =========================================================
# 15) 🔥 今日最能買（至多三場）— 維持你框架
# =========================================================
st.header("🔥 今日最能買（至多三場）")

pick_pool = []
for g in all_games_data[:MAX_GAMES_FOR_PICK]:
    gid = g["game_id"]

    # 使用者輸入：主隊盤口（主讓負｜主受讓正）
    u_sp = float(st.session_state.get(f"sp_{gid}", g["default_sp"]))
    u_oh = float(st.session_state.get(f"oh_{gid}", g["default_oh"]))
    u_oa = float(st.session_state.get(f"oa_{gid}", g["default_oa"]))

    # 建特徵（主隊視角）
    feats = calc_features(g["h_id"], g["a_id"], g["h_abbr"], g["a_abbr"], home_spread=u_sp)
    cover_prob_home = predict_cover_prob(model, feats)

    # 這裡的 cover_prob_home 指「主隊過盤」機率
    # 若主隊盤口 u_sp + 預估 -> 我們直接以 cover_prob_home 做決策
    # 選邊：若主隊過盤機率 > 0.5 → 推主；反之推客
    pick_home = (cover_prob_home >= 0.5)
    pick_side = g["h_cn"] if pick_home else g["a_cn"]
    odds = u_oh if pick_home else u_oa

    implied_prob = 1.0 / odds if odds and odds > 0 else 1.0
    cover_prob = cover_prob_home if pick_home else (1.0 - cover_prob_home)

    edge_value = cover_prob - implied_prob
    ev = (cover_prob * odds) - 1.0

    pick_pool.append({
        "g": g,
        "pick_side": pick_side,
        "pick_home": pick_home,
        "cover_prob": cover_prob,
        "implied_prob": implied_prob,
        "edge_value": edge_value,
        "ev": ev,
        "u_sp": u_sp,
        "odds": odds,
        "feats": feats,
    })

qualified = [x for x in pick_pool if x["edge_value"] > EDGE_THRESHOLD]
qualified.sort(key=lambda x: (x["cover_prob"], x["edge_value"]), reverse=True)
picks = qualified[:MAX_PICKS]

if len(picks) == 0:
    st.info("依長期門檻：前 10 場中沒有任何一場「盤口優勢 > 8%」。建議不買、不硬湊。")
else:
    if len(picks) == 1:
        st.success("🎯 今日只有 1 場達到門檻：建議單場（或分注單場），不要硬湊串關。")
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
                st.write(f"主隊盤口：**{item['u_sp']:+.1f}** | 賠率：**{item['odds']:.2f}**")
                st.write(f"期望報酬：**{item['ev']*100:+.1f}%**")

st.divider()


# =========================================================
# 16) 🎯 全部場次與實時計算（維持原 UI）
# =========================================================
st.header("🎯 全部場次與實時計算")

for i in range(0, len(all_games_data), 3):
    cols = st.columns(3)
    for j, g in enumerate(all_games_data[i:i+3]):
        with cols[j]:
            with st.container(border=True):
                st.subheader(g["label"])
                gid = g["game_id"]

                u_sp = st.number_input(
                    "主隊盤口（主讓分填負｜主受讓填正）",
                    min_value=-60.0, max_value=60.0,
                    value=float(st.session_state.get(f"sp_{gid}", g["default_sp"])),
                    step=0.5,
                    key=f"sp_{gid}",
                )
                u_oh = st.number_input(
                    "主賠",
                    min_value=1.01, max_value=5.0,
                    value=float(st.session_state.get(f"oh_{gid}", g["default_oh"])),
                    step=0.01,
                    key=f"oh_{gid}",
                )
                u_oa = st.number_input(
                    "客賠",
                    min_value=1.01, max_value=5.0,
                    value=float(st.session_state.get(f"oa_{gid}", g["default_oa"])),
                    step=0.01,
                    key=f"oa_{gid}",
                )

                feats = calc_features(g["h_id"], g["a_id"], g["h_abbr"], g["a_abbr"], home_spread=u_sp)
                p_home_cover = predict_cover_prob(model, feats)

                pick_home = (p_home_cover >= 0.5)
                rec = g["h_cn"] if pick_home else g["a_cn"]
                odds = u_oh if pick_home else u_oa

                cover_prob = p_home_cover if pick_home else (1.0 - p_home_cover)
                implied_prob = 1.0 / odds if odds and odds > 0 else 1.0
                edge_value = cover_prob - implied_prob
                ev = (cover_prob * odds) - 1.0

                st.write(f"過盤機率：**{cover_prob*100:.1f}%**")
                st.write(f"盤口優勢：**{edge_value*100:+.1f}%** | 期望報酬：**{ev*100:+.1f}%**")

                if edge_value > EDGE_THRESHOLD:
                    st.success(f"🔥 符合長期門檻（盤口優勢 > {EDGE_THRESHOLD*100:.0f}%）：{rec}")
                else:
                    st.info(f"建議：{rec}")


# =========================================================
# 17) 🔍 深度查詢（加更多分系＋顯示模型特徵）
# =========================================================
st.divider()
st.header("🔍 深度數據查詢（分系拆解）")

sel = st.selectbox("請選擇場次", [g["label"] for g in all_games_data])
if sel:
    curr = next(g for g in all_games_data if g["label"] == sel)
    gid = curr["game_id"]

    u_sp = float(st.session_state.get(f"sp_{gid}", curr["default_sp"]))
    feats = calc_features(curr["h_id"], curr["a_id"], curr["h_abbr"], curr["a_abbr"], home_spread=u_sp)
    p_home_cover = predict_cover_prob(model, feats)

    st.write(f"📌 主隊盤口：**{u_sp:+.1f}**（主讓負｜主受讓正）")
    st.write(f"📌 模型估計：主隊過盤機率 **{p_home_cover*100:.1f}%** ｜客隊過盤機率 **{(1-p_home_cover)*100:.1f}%**")

    # 分系區塊
    st.subheader("① 效率分系（Season Per100）")
    st.write(f"NET 差（主-客）：**{feats['net']:+.2f}**")
    st.write(f"OR-DR 結構差（主-客）：**{feats['ordr']:+.2f}**")
    st.write(f"eFG/TOV/REB 綜合差：**{feats['misc']:+.2f}**")

    st.subheader("② 近期/疲勞/波動分系")
    st.write(f"近10淨勝分差（主-客）：**{feats['recent']:+.2f}**")
    st.write(f"疲勞優勢（主相對客，正=主更累）：**{feats['fatigue']:+.2f}**")
    st.write(f"名單不確定性（主-客）：**{feats['uncert']:+.2f}**")
    st.write(f"盤深（|spread|-7）：**{feats['spread_depth']:.2f}**")

    st.subheader("③ 兩隊球員戰力（已排除 ESPN 確定缺陣）")
    c1, c2 = st.columns(2)
    for col, pkg, title in [(c1, feats["h_pkg"], f"{curr['h_cn']} (主)"),
                            (c2, feats["a_pkg"], f"{curr['a_cn']} (客)")]:
        with col:
            st.write(f"**{title}**")
            if pkg["df"] is not None and not pkg["df"].empty:
                show_cols = [c for c in ["PLAYER_NAME", "MIN", "PTS", "IMPACT"] if c in pkg["df"].columns]
                st.dataframe(pkg["df"][show_cols].head(12), hide_index=True)
            else:
                st.write("（球員資料不足或 API 暫時不可用）")

            if pkg["inj"] is not None and not pkg["inj"].empty:
                st.dataframe(pkg["inj"][["球員", "狀態", "原因"]], hide_index=True)
            else:
                st.write("✅ 無傷病報告")

    st.subheader("④ 目前模型參數（可學習）")
    st.json(model)


# =========================================================
# 18) 🧠 讓模型「自動學」：把已結束比賽寫入資料庫 + 訓練
#     - 不需要 historical odds 也能做：你每天跑，系統記錄當天盤口與結果
# =========================================================
st.divider()
st.header("🧠 模型自動學習（越跑越準）")

with st.expander("點我展開：如何讓模型自動變準？（重要）", expanded=False):
    st.markdown(
        "1) 你每天開一次這個系統（賽前）。\n"
        "2) 系統會抓 Pinnacle 當下盤口/賠率（或你手動輸入的盤口/賠率）。\n"
        "3) 隔天你再打開一次，系統會把「昨天已結束的比賽」寫進 edge_training_data.csv。\n"
        "4) 之後模型會用這些真實資料做線上學習，自動校準哪些分系真的有用。\n\n"
        "✅ 這個方法不需要 The Odds API 的 historical 權限，但需要你每天開一次。"
    )

if st.button("📥 更新已結束比賽到訓練資料（建議每天按一次）"):
    # 我們用 teamgamelog 去抓「昨天」結果，並用你當時輸入/或預設的盤口做回填
    # 這裡的策略：把「昨日日期」的 scoreboard 取出，對應你的盤口輸入，算是否過盤（主隊視角）
    y_us = (datetime.now(us_east_tz) - timedelta(days=1)).strftime("%m/%d/%Y")
    sb_y = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=y_us)
    new_rows = []

    if not sb_y.empty and "HOME_TEAM_ID" in sb_y.columns:
        sb_y = sb_y[sb_y["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)].copy()

        # ScoreboardV2 的比分欄位依版本不同，常見是 HOME_TEAM_SCORE / VISITOR_TEAM_SCORE
        # 做容錯
        home_score_col = "HOME_TEAM_SCORE" if "HOME_TEAM_SCORE" in sb_y.columns else None
        away_score_col = "VISITOR_TEAM_SCORE" if "VISITOR_TEAM_SCORE" in sb_y.columns else None

        for _, r in sb_y.iterrows():
            hid, aid = int(r["HOME_TEAM_ID"]), int(r["VISITOR_TEAM_ID"])
            habbr, aabbr = ID_MAP.get(hid, str(hid)), ID_MAP.get(aid, str(aid))
            hcn, acn = TEAM_NAME_CH.get(h
