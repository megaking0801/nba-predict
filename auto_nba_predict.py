import streamlit as st
from nba_api.stats.endpoints import (
    leaguegamefinder, scoreboardv2, leaguedashplayerstats,
    leaguedashteamstats, leaguehustlestatsteam, leaguedashptstats,
    synergyplaytypes
)
from nba_api.stats.static import teams

import pandas as pd
import numpy as np
import xgboost as xgb
import pytz, warnings, requests, unicodedata, time, re
from datetime import datetime, timedelta

# ========= 1) 基本設定 =========
warnings.filterwarnings("ignore")
tw_tz = pytz.timezone("Asia/Taipei")
us_east_tz = pytz.timezone("US/Eastern")

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

# ✅ stats.nba.com 常需要的 headers（重點：User-Agent + Referer + Origin）
NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
}

st.set_page_config(page_title="NBA 數據專家 v8.1（回到stats + NBA官方傷病）", layout="wide")
st.title("🏀 NBA 數據專家 v8.1（回到 stats API + NBA 官方傷病）")

# ========= 2) 工具函數 =========
def normalize_name(name):
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

def fetch_safe_df(endpoint_class, retry=3, sleep_base=0.8, **kwargs):
    """
    對 nba_api endpoint 加上：
    - headers / timeout
    - retry + 簡單退避
    """
    # 強制塞 headers/timeout（避免你某些 endpoint 忘了傳）
    kwargs.setdefault("headers", NBA_HEADERS)
    kwargs.setdefault("timeout", 25)

    last_err = None
    for i in range(retry):
        try:
            instance = endpoint_class(**kwargs)
            raw = instance.get_dict()
            res = raw["resultSets"][0] if "resultSets" in raw else raw["resultSet"]
            df = pd.DataFrame(res["rowSet"], columns=res["headers"])
            # 部分 endpoint TEAM_ID 型別會變 float/obj
            if "TEAM_ID" in df.columns:
                df["TEAM_ID"] = pd.to_numeric(df["TEAM_ID"], errors="coerce").fillna(0).astype(int)
            return df
        except Exception as e:
            last_err = e
            time.sleep(sleep_base * (2 ** i))
    # 全部失敗 -> 空表
    return pd.DataFrame()

# ========= 3) NBA 官方 Injury Report（PDF）=========
OFFICIAL_INJURY_PAGE = "https://official.nba.com/nba-injury-report-2025-26-season/"
PDF_PAT = re.compile(
    r"(https?://[^\s\"']*Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})(AM|PM)\.pdf)"
)

# 你要「確定報銷」：至少 Out/Doubtful 先當作不能打
OUTLIKE = {"Out", "Doubtful"}

# 勝率修正權重（百分點）：你可自行調
STATUS_POINTS = {
    "Out": 3.0,
    "Doubtful": 2.0,
    "Questionable": 1.2,
    "Probable": 0.6,
    "Available": 0.0
}

TEAM_FULLNAME_TO_ABBR = {}
for t in teams.get_teams():
    TEAM_FULLNAME_TO_ABBR[t["full_name"]] = t["abbreviation"]
# 常見變體
TEAM_FULLNAME_TO_ABBR["LA Clippers"] = "LAC"
TEAM_FULLNAME_TO_ABBR["LA Lakers"] = "LAL"
TEAM_FULLNAME_TO_ABBR["Los Angeles Clippers"] = "LAC"
TEAM_FULLNAME_TO_ABBR["Los Angeles Lakers"] = "LAL"

def _parse_pdf_time(hh: str, mm: str, ap: str) -> int:
    h = int(hh); m = int(mm)
    if ap.upper() == "PM" and h != 12:
        h += 12
    if ap.upper() == "AM" and h == 12:
        h = 0
    return h * 100 + m

@st.cache_data(ttl=1800)
def find_latest_injury_pdf_url(date_yyyy_mm_dd: str):
    try:
        html = requests.get(OFFICIAL_INJURY_PAGE, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).text
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

@st.cache_data(ttl=1800)
def download_pdf_bytes(url: str):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code != 200:
            return None
        return r.content
    except:
        return None

def parse_official_injury_pdf(pdf_bytes: bytes) -> pd.DataFrame:
    """
    輸出欄位：TEAM_ABBR, PLAYER, STATUS, REASON, IS_OUT
    """
    try:
        from pypdf import PdfReader
        from io import BytesIO
        reader = PdfReader(BytesIO(pdf_bytes))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        return pd.DataFrame(columns=["TEAM_ABBR", "PLAYER", "STATUS", "REASON", "IS_OUT"])

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

        abbr = TEAM_FULLNAME_TO_ABBR.get(team_name)
        if not abbr:
            abbr = next((a for k, a in TEAM_FULLNAME_TO_ABBR.items() if k in team_name), None)
        if not abbr:
            continue

        rows.append({
            "TEAM_ABBR": abbr,
            "PLAYER": player,
            "STATUS": status,
            "REASON": reason,
            "IS_OUT": status in OUTLIKE
        })

    return pd.DataFrame(rows)

@st.cache_data(ttl=900)
def get_official_injury_report(date_yyyy_mm_dd: str) -> pd.DataFrame:
    url = find_latest_injury_pdf_url(date_yyyy_mm_dd)
    if not url:
        return pd.DataFrame(columns=["TEAM_ABBR", "PLAYER", "STATUS", "REASON", "IS_OUT"])
    pdf_bytes = download_pdf_bytes(url)
    if not pdf_bytes:
        return pd.DataFrame(columns=["TEAM_ABBR", "PLAYER", "STATUS", "REASON", "IS_OUT"])
    return parse_official_injury_pdf(pdf_bytes)

def get_team_injury_impact(team_abbr: str, inj_df: pd.DataFrame):
    """
    回傳：
    - impact: float（百分點）
    - details: list[str]
    - out_name_set: set[str]（normalize後）
    """
    if inj_df.empty:
        return 0.0, [], set()

    sub = inj_df[inj_df["TEAM_ABBR"] == team_abbr]
    if sub.empty:
        return 0.0, [], set()

    impact = 0.0
    details = []
    out_names = set()

    for _, r in sub.iterrows():
        player = str(r["PLAYER"])
        status = str(r["STATUS"])
        reason = str(r["REASON"])
        pen = float(STATUS_POINTS.get(status, 0.0))
        if pen <= 0:
            continue

        icon = "❌" if status in OUTLIKE else "⚠️"
        details.append(f"{icon} {player}：{status} 影響 -{pen:.1f}｜{reason}")
        impact += pen

        if status in OUTLIKE:
            out_names.add(normalize_name(player))

    impact = min(18.0, impact)
    return impact, details, out_names

# ========= 4) 數據核心（保留 v8.0，但加 headers/retry + 官方傷病排除）=========
@st.cache_data(ttl=3600, show_spinner=True)
def load_all_data_v81():
    nba_ids = [t["id"] for t in teams.get_teams()]
    S, ST = "2025-26", "Regular Season"

    # --- 球員資料 ---
    ps_raw = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed="PerGame")
    ps_adv = fetch_safe_df(leaguedashplayerstats.LeagueDashPlayerStats, season=S, per_mode_detailed="PerGame", measure_type_detailed_defense="Advanced")

    ps_full = pd.DataFrame()
    if not ps_raw.empty and not ps_adv.empty:
        keep_raw = [c for c in ["PLAYER_ID", "TEAM_ID", "PLAYER_NAME", "PTS", "REB", "AST"] if c in ps_raw.columns]
        keep_adv = [c for c in ["PLAYER_ID", "TS_PCT"] if c in ps_adv.columns]
        ps_full = pd.merge(ps_raw[keep_raw], ps_adv[keep_adv], on="PLAYER_ID", how="inner")

    # --- 團隊資料 maps ---
    df_adv = fetch_safe_df(leaguedashteamstats.LeagueDashTeamStats, season=S, measure_type_detailed_defense="Advanced")
    df_hustle = fetch_safe_df(leaguehustlestatsteam.LeagueHustleStatsTeam, season=S, per_mode_time="PerGame")
    df_spd = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type="SpeedDistance", per_mode_simple="PerGame")
    df_pass = fetch_safe_df(leaguedashptstats.LeagueDashPtStats, season=S, pt_measure_type="Passing", per_mode_simple="PerGame")
    df_trans = fetch_safe_df(synergyplaytypes.SynergyPlayTypes, play_type_nullable="Transition", player_or_team_abbreviation="T", season=S, season_type_all_star=ST)

    def to_map(df, cols):
        if df.empty or "TEAM_ID" not in df.columns:
            return {}
        cols = [c for c in cols if c in df.columns]
        if not cols:
            return {}
        return df.set_index("TEAM_ID")[cols].to_dict("index")

    maps = {
        "adv": to_map(df_adv, ["OFF_RATING", "DEF_RATING", "PACE"]),
        "hustle": to_map(df_hustle, ["DEFLECTIONS", "CONTESTED_SHOTS"]),
        "spd": to_map(df_spd, ["DIST_MILES", "AVG_SPEED"]),
        "pass": to_map(df_pass, ["PASSES_MADE"]),
        "trans": to_map(df_trans, ["PPP"])
    }

    # --- GameFinder（用來訓練勝率/分差）---
    gf_raw = fetch_safe_df(leaguegamefinder.LeagueGameFinder, season_nullable=S)
    gf = gf_raw[gf_raw.get("TEAM_ID", pd.Series([], dtype=int)).isin(nba_ids)].copy() if not gf_raw.empty else pd.DataFrame()

    if gf.empty:
        # 讓 UI 能活著：但模型會用 fallback
        clf = None
        reg = None
        feats = []
    else:
        gf["GAME_DATE"] = pd.to_datetime(gf["GAME_DATE"])
        gf["WIN_BIN"] = gf["WL"].apply(lambda x: 1 if x == "W" else 0)
        gf = gf.sort_values(["TEAM_ID", "GAME_DATE"])
        gf["REST_DAYS"] = gf.groupby("TEAM_ID")["GAME_DATE"].diff().dt.days.fillna(3)

        # ✅ 保留你原本想要的多特徵，但這裡只用「可穩定取到」的 team map
        def get_team_feat(tid, group, key, default=0.0):
            return float(maps.get(group, {}).get(int(tid), {}).get(key, default))

        gf["T_ORTG"] = gf["TEAM_ID"].apply(lambda x: get_team_feat(x, "adv", "OFF_RATING", 112.0))
        gf["T_DRTG"] = gf["TEAM_ID"].apply(lambda x: get_team_feat(x, "adv", "DEF_RATING", 112.0))
        gf["T_PACE"] = gf["TEAM_ID"].apply(lambda x: get_team_feat(x, "adv", "PACE", 99.0))
        gf["T_TRANS"] = gf["TEAM_ID"].apply(lambda x: get_team_feat(x, "trans", "PPP", 1.10))
        gf["T_DEFL"] = gf["TEAM_ID"].apply(lambda x: get_team_feat(x, "hustle", "DEFLECTIONS", 15.0))

        feats = ["REST_DAYS", "T_ORTG", "T_DRTG", "T_PACE", "T_TRANS", "T_DEFL"]
        train = gf.dropna(subset=["WIN_BIN"]).copy()
        X = train[feats].fillna(0)
        y = train["WIN_BIN"].astype(int)

        # 防呆：資料太少就不要訓練
        if len(train) < 80:
            clf = None
            reg = None
        else:
            clf = xgb.XGBClassifier(
                n_estimators=250,
                max_depth=4,
                learning_rate=0.05,
                subsample=0.9,
                colsample_bytree=0.9,
                eval_metric="logloss",
                random_state=42
            ).fit(X, y)

            reg = xgb.XGBRegressor(
                n_estimators=300,
                max_depth=4,
                learning_rate=0.05,
                subsample=0.9,
                colsample_bytree=0.9,
                random_state=42
            ).fit(X, train["PLUS_MINUS"].fillna(0))

    return clf, reg, gf, ps_full, feats, maps, datetime.now(tw_tz).strftime("%H:%M:%S")

clf, reg, gf, ps_full, feats, maps, last_update = load_all_data_v81()

# ========= 5) UI：照你 v8.0 走 scoreboardv2（stats）=========
nba_now = datetime.now(us_east_tz)
dates_nba = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime("%m/%d") for d in dates_nba])

# 取得官方傷病：用「美東日期」去抓 pdf
injury_df_today = get_official_injury_report(datetime.now(us_east_tz).strftime("%Y-%m-%d"))

id_to_abbr = {t["id"]: t["abbreviation"] for t in teams.get_teams()}

for i, tab in enumerate(tabs):
    with tab:
        search_date = dates_nba[i].strftime("%m/%d/%Y")
        sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=search_date)

        if sb.empty:
            st.info(f"📅 美國時間 {dates_nba[i].strftime('%Y-%m-%d')} 暫無賽程/或 stats 暫時回傳空資料")
            continue

        # 用該頁的日期去抓對應 injury report
        inj_df = get_official_injury_report(dates_nba[i].strftime("%Y-%m-%d"))

        game_list = []
        for _, row in sb.iterrows():
            h_id, a_id = int(row["HOME_TEAM_ID"]), int(row["VISITOR_TEAM_ID"])
            h_abbr, a_abbr = id_to_abbr.get(h_id), id_to_abbr.get(a_id)
            if not h_abbr or not a_abbr:
                continue
            game_list.append({
                "label": f"{TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}",
                "h_id": h_id, "a_id": a_id,
                "h_abbr": h_abbr, "a_abbr": a_abbr
            })

        if not game_list:
            st.info("📅 找不到可解析的場次")
            continue

        # 賠率輸入
        st.subheader("💰 當日賠率批次輸入（計算 Edge）")
        with st.expander("展開輸入當前運彩賠率", expanded=True):
            input_odds = {}
            o_cols = st.columns(3)
            for idx, g in enumerate(game_list):
                with o_cols[idx % 3]:
                    st.write(f"**{g['label']}**")
                    oh = st.number_input(f"🏠 {TEAM_NAME_CH.get(g['h_abbr'])}", value=1.75, step=0.01, key=f"oh_{i}_{idx}")
                    oa = st.number_input(f"✈️ {TEAM_NAME_CH.get(g['a_abbr'])}", value=1.75, step=0.01, key=f"oa_{i}_{idx}")
                    input_odds[idx] = (oh, oa)

        analysis_data = []
        for idx, g in enumerate(game_list):
            h_abbr, a_abbr = g["h_abbr"], g["a_abbr"]

            # injury 修正 & out名單（用於 Top6 排除）
            h_imp, h_det, h_out_names = get_team_injury_impact(h_abbr, inj_df)
            a_imp, a_det, a_out_names = get_team_injury_impact(a_abbr, inj_df)

            # 模型（若不可用 -> 50% baseline）
            if clf is None or reg is None or gf.empty:
                base_p = 50.0
                base_m = 0.0
            else:
                h_last = gf[gf["TEAM_ABBREVIATION"] == h_abbr].tail(1)
                if h_last.empty:
                    base_p = 50.0
                    base_m = 0.0
                else:
                    base_p = float(clf.predict_proba(h_last[feats].fillna(0))[0][1] * 100)
                    base_m = float(reg.predict(h_last[feats].fillna(0))[0])

            # ✅ 勝率修正：主隊 -h_imp + a_imp
            final_p_h = clamp(base_p - h_imp + a_imp, 5, 95)
            final_m_h = base_m - (h_imp * 0.35) + (a_imp * 0.35)

            oh, oa = input_odds[idx]
            imp_h = (1/oh) / ((1/oh) + (1/oa)) * 100
            imp_a = (1/oa) / ((1/oh) + (1/oa)) * 100
            edge_h = final_p_h - imp_h
            edge_a = (100 - final_p_h) - imp_a

            analysis_data.append({
                "label": g["label"],
                "h_id": g["h_id"], "a_id": g["a_id"],
                "h_abbr": h_abbr, "a_abbr": a_abbr,
                "h_ch": TEAM_NAME_CH.get(h_abbr), "a_ch": TEAM_NAME_CH.get(a_abbr),
                "final_p_h": final_p_h,
                "final_m_h": final_m_h,
                "edge_h": edge_h, "edge_a": edge_a,
                "odds_h": oh, "odds_a": oa,
                "h_det": h_det, "a_det": a_det,
                "h_out": h_out_names, "a_out": a_out_names
            })

        # Top3 推薦
        st.divider()
        st.subheader("🔥 AI 推薦串關最優三場（模型/或50% baseline + 官方傷病修正）")
        recs = []
        for d in analysis_data:
            if d["edge_h"] >= d["edge_a"]:
                recs.append({"pick": d["h_ch"], "edge": d["edge_h"], "match": d["label"], "odds": d["odds_h"]})
            else:
                recs.append({"pick": d["a_ch"], "edge": d["edge_a"], "match": d["label"], "odds": d["odds_a"]})
        top_3 = sorted(recs, key=lambda x: x["edge"], reverse=True)[:3]
        rc1, rc2, rc3 = st.columns(3)
        for idx, r in enumerate(top_3):
            with [rc1, rc2, rc3][idx]:
                st.success(f"**No.{idx+1} {r['pick']}**\n\n{r['match']}\n\n價值: +{r['edge']:.1f}% | 賠率: {r['odds']:.2f}")

        # 單場詳細
        st.divider()
        sel_label = st.selectbox("🔍 選擇場次查看詳細", [d["label"] for d in analysis_data], key=f"sel_{i}")
        curr = next(d for d in analysis_data if d["label"] == sel_label)

        st.markdown(f"### 🏟️ {sel_label}")
        c1, c2, c3 = st.columns(3)
        c1.metric(curr["h_ch"], f"{curr['final_p_h']:.1f}%", f"預測分差: {curr['final_m_h']:+.1f}")
        c2.metric(curr["a_ch"], f"{100-curr['final_p_h']:.1f}%", f"預測分差: {-curr['final_m_h']:+.1f}")
        c3.metric("AI 建議贏家", curr["h_ch"] if curr["final_p_h"] >= 50 else curr["a_ch"])

        # 傷病
        st.subheader("🚑 官方傷病（NBA Injury Report PDF）")
        ic1, ic2 = st.columns(2)
        with ic1:
            st.write(f"**{curr['h_ch']}**")
            if curr["h_det"]:
                for x in curr["h_det"]:
                    st.write(x)
            else:
                st.success("官方 injury report 未列出或無顯著影響")
        with ic2:
            st.write(f"**{curr['a_ch']}**")
            if curr["a_det"]:
                for x in curr["a_det"]:
                    st.write(x)
            else:
                st.success("官方 injury report 未列出或無顯著影響")

        # 團隊數據對比（用 maps）
        def get_m(group, tid, key, default=0.0):
            return float(maps.get(group, {}).get(int(tid), {}).get(key, default))

        st.subheader("📊 團隊深度數據對比（場均/效率）")
        st.table(pd.DataFrame({
            "指標": ["進攻效率(OffRtg)", "防守效率(DefRtg)", "節奏(Pace)", "轉換進攻PPP", "撥球(Defl)", "干擾投籃(Contested)", "跑動里程(mi)", "場均傳球"],
            curr["h_ch"]: [
                f"{get_m('adv', curr['h_id'], 'OFF_RATING', 0):.1f}",
                f"{get_m('adv', curr['h_id'], 'DEF_RATING', 0):.1f}",
                f"{get_m('adv', curr['h_id'], 'PACE', 0):.1f}",
                f"{get_m('trans', curr['h_id'], 'PPP', 0):.2f}",
                f"{get_m('hustle', curr['h_id'], 'DEFLECTIONS', 0):.1f}",
                f"{get_m('hustle', curr['h_id'], 'CONTESTED_SHOTS', 0):.1f}",
                f"{get_m('spd', curr['h_id'], 'DIST_MILES', 0):.2f}",
                f"{get_m('pass', curr['h_id'], 'PASSES_MADE', 0):.1f}",
            ],
            curr["a_ch"]: [
                f"{get_m('adv', curr['a_id'], 'OFF_RATING', 0):.1f}",
                f"{get_m('adv', curr['a_id'], 'DEF_RATING', 0):.1f}",
                f"{get_m('adv', curr['a_id'], 'PACE', 0):.1f}",
                f"{get_m('trans', curr['a_id'], 'PPP', 0):.2f}",
                f"{get_m('hustle', curr['a_id'], 'DEFLECTIONS', 0):.1f}",
                f"{get_m('hustle', curr['a_id'], 'CONTESTED_SHOTS', 0):.1f}",
                f"{get_m('spd', curr['a_id'], 'DIST_MILES', 0):.2f}",
                f"{get_m('pass', curr['a_id'], 'PASSES_MADE', 0):.1f}",
            ]
        }))

        # ✅ Top6 核心球員（排除 OUT/DOUBTFUL）
        st.subheader("🚀 核心球員 Top 6（已排除確定不能打：Out/Doubtful）")
        if ps_full.empty:
            st.info("球員資料抓取不到（leaguedashplayerstats 可能暫時空/被限流）")
        else:
            p1, p2 = st.columns(2)

            for tid, team_ch, out_set, col in [
                (curr["h_id"], curr["h_ch"], curr["h_out"], p1),
                (curr["a_id"], curr["a_ch"], curr["a_out"], p2),
            ]:
                with col:
                    st.write(f"**{team_ch}**")
                    df_team = ps_full[ps_full["TEAM_ID"] == int(tid)].copy()
                    # 排除 out players
                    df_team["NORM"] = df_team["PLAYER_NAME"].apply(normalize_name)
                    df_team = df_team[~df_team["NORM"].isin(out_set)].drop(columns=["NORM"], errors="ignore")

                    df_team = df_team.sort_values("PTS", ascending=False).head(6)
                    if df_team.empty:
                        st.info("可用球員資料不足（或 Top scorer 全被列為 Out/Doubtful）")
                    else:
                        show_cols = ["PLAYER_NAME", "PTS", "REB", "AST", "TS_PCT"]
                        show_cols = [c for c in show_cols if c in df_team.columns]
                        st.dataframe(
                            df_team[show_cols].rename(columns={
                                "PLAYER_NAME": "姓名", "PTS": "得分", "REB": "籃板", "AST": "助攻", "TS_PCT": "真實命中%"
                            }).style.format({
                                "得分": "{:.1f}", "籃板": "{:.1f}", "助攻": "{:.1f}", "真實命中%": "{:.1%}"
                            }),
                            hide_index=True
                        )

st.sidebar.info(f"🕒 更新時間：{last_update}")
st.sidebar.caption("資料來源：NBA stats endpoints（ScoreboardV2/LeagueGameFinder/DashStats）+ NBA 官方 Injury Report（PDF）")
