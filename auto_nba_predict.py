import streamlit as st
import pandas as pd
import numpy as np
import pytz, warnings, requests, re, unicodedata, time, random
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from nba_api.stats.static import teams
from nba_api.stats.endpoints import (
    leaguegamefinder,
    scoreboardv2,
    leaguedashteamstats,
    leaguedashplayerstats
)

import xgboost as xgb


# =========================
# 0) 設定
# =========================
warnings.filterwarnings("ignore")

tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")  # NBA 多數資料以 ET 為主

TEAM_NAME_CH = {
    'ATL': '亞特蘭大老鷹', 'BKN': '布魯克林籃網', 'BOS': '波士頓塞爾提克',
    'CHA': '夏洛特黃蜂', 'CHI': '芝加哥公牛', 'CLE': '克里夫蘭騎士',
    'DAL': '達拉斯獨行俠', 'DEN': '丹佛金塊', 'DET': '底特律活塞',
    'GSW': '金州勇士', 'HOU': '休士頓火箭', 'IND': '印第安納溜馬',
    'LAC': '洛杉磯快艇', 'LAL': '洛杉磯湖人', 'MEM': '曼非斯灰熊',
    'MIA': '邁阿密熱火', 'MIL': '密爾瓦基公鹿', 'MIN': '明尼蘇達灰狼',
    'NOP': '紐奧良鵜鶘', 'NYK': '紐約尼克', 'OKC': '奧克拉荷馬雷霆',
    'ORL': '奧蘭多魔術', 'PHI': '費城 76 人', 'PHX': '鳳凰城太陽',
    'POR': '波特蘭開拓者', 'SAC': '沙加邁度國王', 'SAS': '聖安東尼奧馬刺',
    'TOR': '多倫多暴龍', 'UTA': '猶他爵士', 'WAS': '華盛頓巫師'
}

NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
}

st.set_page_config(page_title="NBA 數據專家 v9.1（官方傷病 Only + Fallback）", layout="wide")
st.title("🏀 NBA 數據專家 v9.1（NBA 官方 Injury Report + Inactive 交叉 + Fallback）")


# =========================
# 1) 小工具
# =========================
def normalize_name(name: str) -> str:
    if not isinstance(name, str):
        return ""
    return (
        unicodedata.normalize("NFD", name)
        .encode("ascii", "ignore")
        .decode("utf-8")
        .lower()
        .replace(".", "")
        .strip()
    )

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def fetch_safe_df(endpoint_class, result_set_name=None, result_set_index=0,
                  max_retries=3, timeout=20, base_sleep=0.8, **kwargs) -> pd.DataFrame:
    """
    nba_api endpoint 抓取：
    - 支援 ScoreboardV2 用 result_set_name 取特定表
    - 遇到錯誤會重試，最後會在 sidebar 顯示錯誤（不再把錯誤吞掉）
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            inst = endpoint_class(headers=NBA_HEADERS, timeout=timeout, **kwargs)
            raw = inst.get_dict()

            if "resultSets" in raw:
                rs_list = raw["resultSets"]
                if result_set_name:
                    rs = next((x for x in rs_list if x.get("name") == result_set_name), None)
                    if rs is None:
                        return pd.DataFrame()
                else:
                    rs = rs_list[result_set_index]
            else:
                rs = raw.get("resultSet")
                if not rs:
                    return pd.DataFrame()

            return pd.DataFrame(rs["rowSet"], columns=rs["headers"])

        except Exception as e:
            last_err = repr(e)
            sleep_s = base_sleep * (2 ** attempt) + random.uniform(0, 0.4)
            time.sleep(sleep_s)

    st.sidebar.error(f"❌ {endpoint_class.__name__} failed: {last_err}")
    return pd.DataFrame()

def build_team_maps():
    tlist = teams.get_teams()
    id_to_abbr = {t["id"]: t["abbreviation"] for t in tlist}
    full_to_abbr = {t["full_name"]: t["abbreviation"] for t in tlist}

    # 常見變體
    full_to_abbr["LA Clippers"] = "LAC"
    full_to_abbr["LA Lakers"] = "LAL"
    full_to_abbr["Los Angeles Clippers"] = "LAC"
    full_to_abbr["Los Angeles Lakers"] = "LAL"
    return id_to_abbr, full_to_abbr

ID_TO_ABBR, FULLNAME_TO_ABBR = build_team_maps()


# =========================
# 2) NBA 官方 Injury Report（PDF）
# =========================
OFFICIAL_INJURY_PAGE = "https://official.nba.com/nba-injury-report-2025-26-season/"

PDF_PAT = re.compile(
    r"(https?://[^\s\"']*Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})(AM|PM)\.pdf)"
)

def _parse_pdf_time(hh: str, mm: str, ap: str) -> int:
    h = int(hh); m = int(mm)
    if ap.upper() == "PM" and h != 12:
        h += 12
    if ap.upper() == "AM" and h == 12:
        h = 0
    return h * 100 + m

@st.cache_data(ttl=1800, show_spinner=False)
def find_latest_injury_pdf_url(date_yyyy_mm_dd: str) -> str | None:
    try:
        html = requests.get(OFFICIAL_INJURY_PAGE, headers={"User-Agent": NBA_HEADERS["User-Agent"]}, timeout=15).text
        matches = PDF_PAT.findall(html)
        candidates = []
        for url, d, hh, mm, ap in matches:
            if d == date_yyyy_mm_dd:
                candidates.append((_parse_pdf_time(hh, mm, ap), url))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[-1][1]
    except:
        return None

@st.cache_data(ttl=1800, show_spinner=False)
def download_pdf_bytes(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers={"User-Agent": NBA_HEADERS["User-Agent"]}, timeout=20)
        if r.status_code != 200:
            return None
        return r.content
    except:
        return None

def parse_official_injury_pdf(pdf_bytes: bytes) -> pd.DataFrame:
    """
    解析 NBA 官方 Injury Report PDF → DataFrame
    欄位：TEAM_ABBR, PLAYER, STATUS, REASON
    """
    try:
        from pypdf import PdfReader
        from io import BytesIO
    except Exception:
        return pd.DataFrame(columns=["TEAM_ABBR", "PLAYER", "STATUS", "REASON"])

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        return pd.DataFrame(columns=["TEAM_ABBR", "PLAYER", "STATUS", "REASON"])

    statuses = ["Available", "Probable", "Questionable", "Doubtful", "Out"]
    status_pat = "(" + "|".join(statuses) + ")"

    row_pat = re.compile(
        r"\b\d{2}/\d{2}/\d{4}\b.*?\b[A-Z]{2,3}@[A-Z]{2,3}\b\s+"
        r"(?P<team>.+?)\s+"
        r"(?P<player>[A-Za-z\-\']+,\s+[A-Za-z\-\'. ]+?)\s+"
        r"(?P<status>" + status_pat + r")\s+"
        r"(?P<reason>.+)$"
    )

    rows = []
    for ln in [x.strip() for x in text.splitlines() if x.strip()]:
        m = row_pat.search(ln)
        if not m:
            continue

        team_name = m.group("team").strip()
        player = m.group("player").strip()
        status = m.group("status").strip()
        reason = m.group("reason").strip()

        abbr = FULLNAME_TO_ABBR.get(team_name)
        if not abbr:
            abbr = next((a for k, a in FULLNAME_TO_ABBR.items() if k in team_name), None)
        if not abbr:
            continue

        rows.append({"TEAM_ABBR": abbr, "PLAYER": player, "STATUS": status, "REASON": reason})

    return pd.DataFrame(rows)

@st.cache_data(ttl=900, show_spinner=False)
def get_official_injury_report(date_yyyy_mm_dd: str) -> pd.DataFrame:
    url = find_latest_injury_pdf_url(date_yyyy_mm_dd)
    if not url:
        return pd.DataFrame(columns=["TEAM_ABBR", "PLAYER", "STATUS", "REASON"])
    pdf_bytes = download_pdf_bytes(url)
    if not pdf_bytes:
        return pd.DataFrame(columns=["TEAM_ABBR", "PLAYER", "STATUS", "REASON"])
    return parse_official_injury_pdf(pdf_bytes)


# =========================
# 3) NBA 官方 InactivePlayers（ScoreboardV2）
# =========================
@st.cache_data(ttl=300, show_spinner=False)
def get_official_inactives_mmddyyyy(game_date_mmddyyyy: str) -> dict:
    df = fetch_safe_df(
        scoreboardv2.ScoreboardV2,
        game_date=game_date_mmddyyyy,
        result_set_name="InactivePlayers",
        max_retries=2,
        timeout=15
    )
    if df.empty or "TEAM_ID" not in df.columns or "PLAYER_NAME" not in df.columns:
        return {}
    out = {}
    for tid, g in df.groupby("TEAM_ID"):
        abbr = ID_TO_ABBR.get(int(tid))
        if not abbr:
            continue
        out[abbr] = set(normalize_name(x) for x in g["PLAYER_NAME"].dropna().astype(str).tolist())
    return out


# =========================
# 4) 核心資料與模型（含 Fallback）
# =========================
def build_team_adv_map(season: str) -> dict:
    df_adv = fetch_safe_df(
        leaguedashteamstats.LeagueDashTeamStats,
        season=season,
        measure_type_detailed_defense="Advanced",
        per_mode_detailed="PerGame",
        max_retries=2,
        timeout=18
    )
    if df_adv.empty or "TEAM_ID" not in df_adv.columns:
        return {}
    keep = [c for c in ["TEAM_ID", "OFF_RATING", "DEF_RATING", "PACE"] if c in df_adv.columns]
    df_adv = df_adv[keep].copy()
    df_adv["TEAM_ID"] = df_adv["TEAM_ID"].astype(int)
    return df_adv.set_index("TEAM_ID").to_dict("index")

def build_player_ppg_db(season: str) -> tuple[dict, pd.DataFrame]:
    ps = fetch_safe_df(
        leaguedashplayerstats.LeagueDashPlayerStats,
        season=season,
        per_mode_detailed="PerGame",
        max_retries=2,
        timeout=18
    )
    if ps.empty or "PLAYER_NAME" not in ps.columns or "PTS" not in ps.columns:
        return {}, pd.DataFrame()
    db = {normalize_name(r["PLAYER_NAME"]): float(r["PTS"]) for _, r in ps.iterrows()}
    return db, ps

def prepare_matchup_training_data(gf_raw: pd.DataFrame, team_adv_map: dict) -> pd.DataFrame:
    df = gf_raw.copy()
    required = {"GAME_ID", "TEAM_ID", "TEAM_ABBREVIATION", "MATCHUP", "WL", "PLUS_MINUS", "GAME_DATE"}
    if not required.issubset(df.columns):
        return pd.DataFrame()

    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"])
    df["IS_HOME"] = df["MATCHUP"].astype(str).str.contains("vs\.")
    df = df.sort_values(["TEAM_ID", "GAME_DATE"])

    df["REST_DAYS"] = df.groupby("TEAM_ID")["GAME_DATE"].diff().dt.days.fillna(3)
    df["REST_DAYS"] = df["REST_DAYS"].clip(lower=0, upper=7)

    home = df[df["IS_HOME"]].copy()
    away = df[~df["IS_HOME"]].copy()

    m = pd.merge(home, away, on="GAME_ID", suffixes=("_H", "_A"), how="inner")

    m["HOME_WIN"] = (m["WL_H"] == "W").astype(int)
    m["HOME_MARGIN"] = pd.to_numeric(m["PLUS_MINUS_H"], errors="coerce").fillna(0.0)

    def get_adv(team_id: int, key: str, default: float):
        return float(team_adv_map.get(int(team_id), {}).get(key, default))

    m["ORTG_H"] = m["TEAM_ID_H"].apply(lambda x: get_adv(x, "OFF_RATING", 112.0))
    m["DRTG_H"] = m["TEAM_ID_H"].apply(lambda x: get_adv(x, "DEF_RATING", 112.0))
    m["PACE_H"] = m["TEAM_ID_H"].apply(lambda x: get_adv(x, "PACE", 99.0))

    m["ORTG_A"] = m["TEAM_ID_A"].apply(lambda x: get_adv(x, "OFF_RATING", 112.0))
    m["DRTG_A"] = m["TEAM_ID_A"].apply(lambda x: get_adv(x, "DEF_RATING", 112.0))
    m["PACE_A"] = m["TEAM_ID_A"].apply(lambda x: get_adv(x, "PACE", 99.0))

    m["REST_DIFF"] = m["REST_DAYS_H"] - m["REST_DAYS_A"]
    m["NET_H"] = m["ORTG_H"] - m["DRTG_H"]
    m["NET_A"] = m["ORTG_A"] - m["DRTG_A"]
    m["NET_DIFF"] = m["NET_H"] - m["NET_A"]
    m["ORTG_DIFF"] = m["ORTG_H"] - m["ORTG_A"]
    m["DRTG_DIFF"] = m["DRTG_A"] - m["DRTG_H"]
    m["PACE_DIFF"] = m["PACE_H"] - m["PACE_A"]

    return m

@st.cache_data(ttl=3600, show_spinner=True)
def load_core_data_v91(season: str):
    team_adv_map = build_team_adv_map(season)
    player_ppg_db, ps_df = build_player_ppg_db(season)

    gf_raw = fetch_safe_df(
        leaguegamefinder.LeagueGameFinder,
        season_nullable=season,
        season_type_nullable="Regular Season",
        league_id_nullable="00",
        max_retries=3,
        timeout=30
    )

    # Fallback：抓不到就不訓練
    if gf_raw.empty:
        return None, None, pd.DataFrame(), team_adv_map, player_ppg_db, ps_df, True

    train = prepare_matchup_training_data(gf_raw, team_adv_map)
    if train.empty:
        return None, None, pd.DataFrame(), team_adv_map, player_ppg_db, ps_df, True

    feats = ["REST_DIFF", "NET_DIFF", "ORTG_DIFF", "DRTG_DIFF", "PACE_DIFF"]
    X = train[feats].fillna(0)
    y = train["HOME_WIN"].astype(int)
    y_margin = train["HOME_MARGIN"].astype(float)

    clf = xgb.XGBClassifier(
        n_estimators=250,
        max_depth=4,
        learning_rate=0.06,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_lambda=1.0,
        random_state=42,
        eval_metric="logloss"
    )
    clf.fit(X, y)

    reg = xgb.XGBRegressor(
        n_estimators=250,
        max_depth=4,
        learning_rate=0.06,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_lambda=1.0,
        random_state=42
    )
    reg.fit(X, y_margin)

    return clf, reg, train, team_adv_map, player_ppg_db, ps_df, False


# =========================
# 5) 傷病影響（官方 PDF + Inactive 覆蓋）
# =========================
STATUS_WEIGHT = {
    "Out": 1.00,
    "Doubtful": 0.75,
    "Questionable": 0.45,
    "Probable": 0.20,
    "Available": 0.00
}

def base_penalty_from_ppg(ppg: float) -> float:
    if ppg >= 28: return 7.0
    if ppg >= 24: return 6.0
    if ppg >= 18: return 4.0
    if ppg >= 12: return 2.5
    if ppg >= 7:  return 1.5
    return 0.8

@st.cache_data(ttl=600, show_spinner=False)
def build_injury_map_for_date(date_mmddyyyy: str, date_yyyy_mm_dd: str) -> dict:
    df_pdf = get_official_injury_report(date_yyyy_mm_dd)
    inactives = get_official_inactives_mmddyyyy(date_mmddyyyy)

    out = {}
    if not df_pdf.empty:
        for abbr, g in df_pdf.groupby("TEAM_ABBR"):
            plist = []
            for _, r in g.iterrows():
                player = str(r["PLAYER"])
                status = str(r["STATUS"])
                reason = str(r["REASON"])

                nm_norm = normalize_name(player)
                if abbr in inactives and nm_norm in inactives[abbr]:
                    status = "Out"
                    reason = f"{reason} | CONFIRMED INACTIVE"

                plist.append({"player": player, "status": status, "reason": reason})
            out[abbr] = plist
    else:
        # PDF 抓不到：至少用 InactivePlayers
        for abbr, nm_set in inactives.items():
            out[abbr] = [{"player": "(Confirmed Inactive)", "status": "Out", "reason": "Scoreboard InactivePlayers"}]

    return out

def injury_impact(team_abbr: str, injury_map: dict, ppg_db: dict):
    plist = injury_map.get(team_abbr, [])
    total = 0.0
    details = []

    for p in plist:
        player = p.get("player", "")
        status = p.get("status", "Available")
        reason = p.get("reason", "")

        w = STATUS_WEIGHT.get(status, 0.0)
        ppg = float(ppg_db.get(normalize_name(player), 0.0))
        base = base_penalty_from_ppg(ppg)
        pen = base * w
        if pen <= 0:
            continue

        icon = "❌" if status in ["Out", "Doubtful"] else "⚠️"
        details.append(f"{icon} {player}（{ppg:.1f} PPG） {status} 影響 -{pen:.1f}｜{reason}")
        total += pen

    total = min(22.0, total)
    return total, details


# =========================
# 6) 對戰特徵（含 Fallback）
# =========================
def get_team_adv(team_id: int, team_adv_map: dict):
    d = team_adv_map.get(int(team_id), {})
    ortg = float(d.get("OFF_RATING", 112.0))
    drtg = float(d.get("DEF_RATING", 112.0))
    pace = float(d.get("PACE", 99.0))
    return ortg, drtg, pace

def last_game_date_for_team(train_df: pd.DataFrame, team_abbr: str) -> pd.Timestamp | None:
    if train_df is None or train_df.empty:
        return None
    # 兩邊都找
    sub_h = train_df[train_df["TEAM_ABBREVIATION_H"] == team_abbr]
    sub_a = train_df[train_df["TEAM_ABBREVIATION_A"] == team_abbr]
    mx = None
    if not sub_h.empty:
        mx = pd.to_datetime(sub_h["GAME_DATE_H"]).max()
    if not sub_a.empty:
        mx2 = pd.to_datetime(sub_a["GAME_DATE_A"]).max()
        mx = mx2 if mx is None else max(mx, mx2)
    return mx

def build_feature_row(home_team_id: int, away_team_id: int,
                      home_abbr: str, away_abbr: str,
                      target_date_et: datetime,
                      team_adv_map: dict,
                      train_df: pd.DataFrame):
    last_h = last_game_date_for_team(train_df, home_abbr)
    last_a = last_game_date_for_team(train_df, away_abbr)

    rest_h = 3
    rest_a = 3
    if last_h is not None:
        rest_h = int((target_date_et.date() - last_h.date()).days)
    if last_a is not None:
        rest_a = int((target_date_et.date() - last_a.date()).days)

    rest_h = int(clamp(rest_h, 0, 7))
    rest_a = int(clamp(rest_a, 0, 7))

    ortg_h, drtg_h, pace_h = get_team_adv(home_team_id, team_adv_map)
    ortg_a, drtg_a, pace_a = get_team_adv(away_team_id, team_adv_map)

    net_h = ortg_h - drtg_h
    net_a = ortg_a - drtg_a

    feat = {
        "REST_DIFF": rest_h - rest_a,
        "NET_DIFF": net_h - net_a,
        "ORTG_DIFF": ortg_h - ortg_a,
        "DRTG_DIFF": drtg_a - drtg_h,
        "PACE_DIFF": pace_h - pace_a
    }
    ctx = {
        "rest_h": rest_h, "rest_a": rest_a,
        "ortg_h": ortg_h, "drtg_h": drtg_h, "pace_h": pace_h, "net_h": net_h,
        "ortg_a": ortg_a, "drtg_a": drtg_a, "pace_a": pace_a, "net_a": net_a
    }
    return feat, ctx


# =========================
# 7) 讀取核心資料
# =========================
SEASON = "2025-26"
clf, reg, train_matchups, team_adv_map, player_ppg_db, ps_df, is_fallback = load_core_data_v91(SEASON)

if is_fallback:
    st.warning("⚠️ LeagueGameFinder 抓不到或訓練資料不足：已啟用「Fallback 模式」：勝率以 50% 為基底，僅用官方傷病做修正。")


# =========================
# 8) UI：三天（明日/今日/昨日）
# =========================
nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime("%m/%d") for d in dates_nba])

for i, tab in enumerate(tabs):
    with tab:
        game_date_mmddyyyy = dates_nba[i].strftime("%m/%d/%Y")
        game_date_yyyy_mm_dd = dates_nba[i].strftime("%Y-%m-%d")

        gh = fetch_safe_df(
            scoreboardv2.ScoreboardV2,
            game_date=game_date_mmddyyyy,
            result_set_name="GameHeader",
            max_retries=2,
            timeout=15
        )

        if gh.empty or "HOME_TEAM_ID" not in gh.columns or "VISITOR_TEAM_ID" not in gh.columns:
            st.info("📅 目前無比賽資訊 / 或官方 scoreboard 暫時抓不到。")
            continue

        injury_map = build_injury_map_for_date(game_date_mmddyyyy, game_date_yyyy_mm_dd)

        games = []
        for _, r in gh.iterrows():
            h_id = int(r["HOME_TEAM_ID"])
            a_id = int(r["VISITOR_TEAM_ID"])
            h_abbr = ID_TO_ABBR.get(h_id)
            a_abbr = ID_TO_ABBR.get(a_id)
            if not h_abbr or not a_abbr:
                continue
            games.append({
                "label": f"{TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}",
                "h_id": h_id, "a_id": a_id,
                "h_abbr": h_abbr, "a_abbr": a_abbr
            })

        if not games:
            st.info("📅 今日無可用賽程資料")
            continue

        # A) 賠率輸入
        st.subheader("💰 當日賠率批次輸入（計算 Edge）")
        with st.expander("展開輸入當前賠率", expanded=True):
            input_odds = {}
            cols = st.columns(3)
            for idx, g in enumerate(games):
                with cols[idx % 3]:
                    st.write(f"**{g['label']}**")
                    oh = st.number_input(f"🏠 {TEAM_NAME_CH.get(g['h_abbr'], g['h_abbr'])}", value=1.75, step=0.01, key=f"oh_{i}_{idx}")
                    oa = st.number_input(f"✈️ {TEAM_NAME_CH.get(g['a_abbr'], g['a_abbr'])}", value=1.75, step=0.01, key=f"oa_{i}_{idx}")
                    input_odds[idx] = (oh, oa)

        # B) 批次計算
        analysis = []
        for idx, g in enumerate(games):
            feat, ctx = build_feature_row(
                g["h_id"], g["a_id"],
                g["h_abbr"], g["a_abbr"],
                target_date_et=dates_nba[i],
                team_adv_map=team_adv_map,
                train_df=train_matchups
            )
            X = pd.DataFrame([feat]).fillna(0)

            if clf is None or reg is None:
                base_p_home = 50.0
                base_margin = 0.0
            else:
                base_p_home = float(clf.predict_proba(X)[0][1] * 100)
                base_margin = float(reg.predict(X)[0])

            h_imp, h_det = injury_impact(g["h_abbr"], injury_map, player_ppg_db)
            a_imp, a_det = injury_impact(g["a_abbr"], injury_map, player_ppg_db)

            final_p_home = clamp(base_p_home - h_imp + a_imp, 5, 95)
            final_margin = base_margin - (h_imp * 0.35) + (a_imp * 0.35)

            oh, oa = input_odds[idx]
            imp_h = (1/oh) / ((1/oh) + (1/oa)) * 100
            imp_a = (1/oa) / ((1/oh) + (1/oa)) * 100
            edge_h = final_p_home - imp_h
            edge_a = (100 - final_p_home) - imp_a

            analysis.append({
                "label": g["label"],
                "h_abbr": g["h_abbr"], "a_abbr": g["a_abbr"],
                "h_id": g["h_id"], "a_id": g["a_id"],
                "h_ch": TEAM_NAME_CH.get(g["h_abbr"], g["h_abbr"]),
                "a_ch": TEAM_NAME_CH.get(g["a_abbr"], g["a_abbr"]),
                "base_p_home": base_p_home,
                "base_margin": base_margin,
                "final_p_home": final_p_home,
                "final_margin": final_margin,
                "h_imp": h_imp, "a_imp": a_imp,
                "h_det": h_det, "a_det": a_det,
                "odds_h": oh, "odds_a": oa,
                "edge_h": edge_h, "edge_a": edge_a,
                "ctx": ctx
            })

        # C) Top 3 推薦
        st.divider()
        st.subheader("🔥 AI 推薦串關最優三場（已含官方傷病修正）")

        picks = []
        for d in analysis:
            if d["edge_h"] >= d["edge_a"]:
                picks.append({"pick": d["h_ch"], "edge": d["edge_h"], "match": d["label"], "odds": d["odds_h"]})
            else:
                picks.append({"pick": d["a_ch"], "edge": d["edge_a"], "match": d["label"], "odds": d["odds_a"]})

        top3 = sorted(picks, key=lambda x: x["edge"], reverse=True)[:3]
        c1, c2, c3 = st.columns(3)
        for j, r in enumerate(top3):
            with [c1, c2, c3][j]:
                st.success(f"**No.{j+1} {r['pick']}**\n\n{r['match']}\n\n價值: +{r['edge']:.1f}% | 賠率: {r['odds']:.2f}")

        # D) 單場詳細
        st.divider()
        sel = st.selectbox("🔍 選擇場次查看詳細", [d["label"] for d in analysis], key=f"sel_{i}")
        curr = next(d for d in analysis if d["label"] == sel)

        st.markdown(f"### 🏟️ {sel}")
        m1, m2, m3 = st.columns(3)
        m1.metric(curr["h_ch"], f"{curr['final_p_home']:.1f}%", f"預測分差: {curr['final_margin']:+.1f}")
        m2.metric(curr["a_ch"], f"{100-curr['final_p_home']:.1f}%", f"預測分差: {-curr['final_margin']:+.1f}")
        m3.metric("AI 建議贏家", curr["h_ch"] if curr["final_p_home"] >= 50 else curr["a_ch"])

        st.subheader("🧠 對戰特徵（主 - 客）")
        st.table(pd.DataFrame({
            "特徵": ["REST_DIFF", "NET_DIFF", "ORTG_DIFF", "DRTG_DIFF", "PACE_DIFF"],
            "值": [
                curr["ctx"]["rest_h"] - curr["ctx"]["rest_a"],
                curr["ctx"]["net_h"] - curr["ctx"]["net_a"],
                curr["ctx"]["ortg_h"] - curr["ctx"]["ortg_a"],
                curr["ctx"]["drtg_a"] - curr["ctx"]["drtg_h"],
                curr["ctx"]["pace_h"] - curr["ctx"]["pace_a"],
            ]
        }))

        st.subheader("📊 團隊概況（官方 Team Advanced）")
        st.table(pd.DataFrame({
            "指標": ["休息天數", "OffRtg", "DefRtg", "NetRtg", "Pace"],
            curr["h_ch"]: [
                curr["ctx"]["rest_h"],
                f"{curr['ctx']['ortg_h']:.1f}",
                f"{curr['ctx']['drtg_h']:.1f}",
                f"{curr['ctx']['net_h']:.1f}",
                f"{curr['ctx']['pace_h']:.1f}"
            ],
            curr["a_ch"]: [
                curr["ctx"]["rest_a"],
                f"{curr['ctx']['ortg_a']:.1f}",
                f"{curr['ctx']['drtg_a']:.1f}",
                f"{curr['ctx']['net_a']:.1f}",
                f"{curr['ctx']['pace_a']:.1f}"
            ]
        }))

        st.subheader("🚑 官方傷病（Injury Report + InactivePlayers）")
        ic1, ic2 = st.columns(2)
        with ic1:
            st.write(f"**{curr['h_ch']}：影響合計 -{curr['h_imp']:.1f}**")
            if curr["h_det"]:
                for x in curr["h_det"]:
                    st.write(x)
            else:
                st.success("無顯著傷病影響（或官方未列入）")

        with ic2:
            st.write(f"**{curr['a_ch']}：影響合計 -{curr['a_imp']:.1f}**")
            if curr["a_det"]:
                for x in curr["a_det"]:
                    st.write(x)
            else:
                st.success("無顯著傷病影響（或官方未列入）")

        # Top scorers（可選：抓得到 ps_df 才顯示）
        if ps_df is not None and not ps_df.empty:
            st.subheader("🚀 核心球員（依 PTS；不會自動排除 Q/D/P，只用於參考）")
            p1, p2 = st.columns(2)
            for tid, name, col in [(curr["h_id"], curr["h_ch"], p1), (curr["a_id"], curr["a_ch"], p2)]:
                with col:
                    st.write(f"**{name}**")
                    sub = ps_df[ps_df["TEAM_ID"] == tid].copy()
                    if sub.empty:
                        st.info("無球員資料")
                    else:
                        sub = sub.sort_values("PTS", ascending=False).head(8)
                        st.dataframe(
                            sub[["PLAYER_NAME", "PTS", "REB", "AST"]].rename(
                                columns={"PLAYER_NAME": "姓名", "PTS": "得分", "REB": "籃板", "AST": "助攻"}
                            ),
                            hide_index=True
                        )

st.sidebar.caption(f"Season：{SEASON}")
st.sidebar.caption(f"🕒 更新時間：{datetime.now(tw_tz).strftime('%Y-%m-%d %H:%M:%S')}")
st.sidebar.info("📌 傷病來源：NBA 官方 Injury Report PDF + 官方 Scoreboard InactivePlayers")
