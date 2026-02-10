import streamlit as st
import pandas as pd
import numpy as np
import pytz, warnings, requests, re, unicodedata
from datetime import datetime, timedelta

from nba_api.stats.static import teams
from nba_api.stats.endpoints import leaguedashteamstats  # 只抓 team advanced
from nba_api.live.nba.endpoints import scoreboard as live_scoreboard  # ✅ 不走 stats.nba.com

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

NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
}

st.set_page_config(page_title="NBA 數據專家 v9.2（Live 賽程 + 官方傷病）", layout="wide")
st.title("🏀 NBA 數據專家 v9.2（Live 賽程 + NBA 官方 Injury Report）")

# -------------------------
# 工具
# -------------------------
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

def sigmoid(z):
    return 1 / (1 + np.exp(-z))

def build_team_maps():
    tlist = teams.get_teams()
    id_to_abbr = {t["id"]: t["abbreviation"] for t in tlist}
    abbr_to_id = {t["abbreviation"]: t["id"] for t in tlist}
    fullname_to_abbr = {t["full_name"]: t["abbreviation"] for t in tlist}
    # 常見變體
    fullname_to_abbr["LA Clippers"] = "LAC"
    fullname_to_abbr["LA Lakers"] = "LAL"
    fullname_to_abbr["Los Angeles Clippers"] = "LAC"
    fullname_to_abbr["Los Angeles Lakers"] = "LAL"
    return id_to_abbr, abbr_to_id, fullname_to_abbr

ID_TO_ABBR, ABBR_TO_ID, FULLNAME_TO_ABBR = build_team_maps()

# -------------------------
# 1) 團隊 Advanced（只抓一次）
# -------------------------
@st.cache_data(ttl=6*3600, show_spinner=True)
def load_team_advanced(season: str) -> dict:
    try:
        df = leaguedashteamstats.LeagueDashTeamStats(
            season=season,
            measure_type_detailed_defense="Advanced",
            per_mode_detailed="PerGame",
            headers=NBA_HEADERS,
            timeout=20
        ).get_data_frames()[0]
    except Exception as e:
        st.sidebar.error(f"❌ Team Advanced 抓取失敗：{repr(e)}")
        return {}

    if df.empty or "TEAM_ID" not in df.columns:
        return {}

    keep = ["TEAM_ID", "OFF_RATING", "DEF_RATING", "PACE"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].copy()
    df["TEAM_ID"] = df["TEAM_ID"].astype(int)
    return df.set_index("TEAM_ID").to_dict("index")

# -------------------------
# 2) NBA 官方 Injury Report（PDF）
# -------------------------
OFFICIAL_INJURY_PAGE = "https://official.nba.com/nba-injury-report-2025-26-season/"
PDF_PAT = re.compile(
    r"(https?://[^\s\"']*Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})(AM|PM)\.pdf)"
)

STATUS_WEIGHT = {
    "Out": 1.00,
    "Doubtful": 0.75,
    "Questionable": 0.45,
    "Probable": 0.20,
    "Available": 0.00
}

def base_penalty_from_ppg(ppg: float) -> float:
    # 你原本那套：用 PPG 當作權重代理（若沒有球員 PPG 資料，會以 0 計）
    if ppg >= 28: return 7.0
    if ppg >= 24: return 6.0
    if ppg >= 18: return 4.0
    if ppg >= 12: return 2.5
    if ppg >= 7:  return 1.5
    return 0.8

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

@st.cache_data(ttl=1800, show_spinner=False)
def download_pdf_bytes(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code != 200:
            return None
        return r.content
    except:
        return None

def parse_official_injury_pdf(pdf_bytes: bytes) -> pd.DataFrame:
    """
    解析 NBA 官方 Injury Report PDF
    欄位：TEAM_ABBR, PLAYER, STATUS, REASON
    """
    try:
        from pypdf import PdfReader
        from io import BytesIO
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

# -------------------------
# 3) Live 賽程（不走 stats）
# -------------------------
@st.cache_data(ttl=120, show_spinner=False)
def get_live_scoreboard(date_yyyy_mm_dd: str) -> list[dict]:
    """
    回傳 list of games:
    {home_abbr, away_abbr, home_id, away_id, label}
    """
    try:
        sb = live_scoreboard.ScoreBoard(game_date=date_yyyy_mm_dd)
        data = sb.get_dict()
        games = data.get("scoreboard", {}).get("games", [])
    except Exception as e:
        st.sidebar.error(f"❌ Live Scoreboard 取得失敗：{repr(e)}")
        return []

    out = []
    for g in games:
        h = g.get("homeTeam", {})
        a = g.get("awayTeam", {})
        h_abbr = h.get("teamTricode")
        a_abbr = a.get("teamTricode")
        if not h_abbr or not a_abbr:
            continue
        out.append({
            "home_abbr": h_abbr,
            "away_abbr": a_abbr,
            "home_id": ABBR_TO_ID.get(h_abbr),
            "away_id": ABBR_TO_ID.get(a_abbr),
            "label": f"{TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}"
        })
    return out

# -------------------------
# 4) 勝率模型（可解釋規則：NetRtg 差 + 主場加成 + 傷病修正）
# -------------------------
def get_team_adv(team_id: int, team_adv_map: dict):
    d = team_adv_map.get(int(team_id), {})
    ortg = float(d.get("OFF_RATING", 112.0))
    drtg = float(d.get("DEF_RATING", 112.0))
    pace = float(d.get("PACE", 99.0))
    net = ortg - drtg
    return ortg, drtg, pace, net

def injury_impact(team_abbr: str, injury_df: pd.DataFrame, ppg_db: dict):
    """
    用官方 injury report 狀態 + PPG 權重估計影響（單位：百分點）
    """
    if injury_df.empty:
        return 0.0, []

    sub = injury_df[injury_df["TEAM_ABBR"] == team_abbr]
    if sub.empty:
        return 0.0, []

    total = 0.0
    details = []
    for _, r in sub.iterrows():
        player = str(r["PLAYER"])
        status = str(r["STATUS"])
        reason = str(r["REASON"])

        w = STATUS_WEIGHT.get(status, 0.0)
        ppg = float(ppg_db.get(normalize_name(player), 0.0))
        base = base_penalty_from_ppg(ppg)
        pen = base * w
        if pen <= 0:
            continue

        icon = "❌" if status in ["Out", "Doubtful"] else "⚠️"
        details.append(f"{icon} {player}（{ppg:.1f} PPG）{status} 影響 -{pen:.1f}｜{reason}")
        total += pen

    total = min(22.0, total)
    return total, details

@st.cache_data(ttl=6*3600, show_spinner=True)
def build_player_ppg_db(season: str) -> dict:
    """
    這裡為了避免 stats 太多端點，先不抓球員 PPG（也能跑）
    你如果確定 leaguedashplayerstats 在你環境可用，再把它加回去
    """
    return {}

def predict_home_win_prob(home_id: int, away_id: int, team_adv_map: dict,
                          home_inj_pen: float, away_inj_pen: float):
    """
    baseline：NetRtg 差 + 主場加成
    - Net diff 轉成 z 值：每 5 NetRtg 差約 ~ 0.6~0.7 的 z（可調）
    - 主場加成：+2.0 net 等價（可調）
    再加傷病修正：home -inj, away +inj
    """
    _, _, _, net_h = get_team_adv(home_id, team_adv_map)
    _, _, _, net_a = get_team_adv(away_id, team_adv_map)

    net_diff = (net_h - net_a)
    home_adv_equiv = 2.0  # 主場加成，等價 net rating
    z = (net_diff + home_adv_equiv) / 5.0  # 5 net 差 -> 1 z 的粗略縮放（可調）

    base_p = sigmoid(z) * 100
    final_p = clamp(base_p - home_inj_pen + away_inj_pen, 5, 95)

    # 粗略分差：net_diff * 0.8（可調），再傷病影響換算
    margin = (net_diff + home_adv_equiv) * 0.8 - (home_inj_pen - away_inj_pen) * 0.35
    return base_p, final_p, margin, net_diff

# -------------------------
# 5) 主流程
# -------------------------
SEASON = "2025-26"
team_adv_map = load_team_advanced(SEASON)
player_ppg_db = build_player_ppg_db(SEASON)

if not team_adv_map:
    st.error("NBA Team Advanced 抓不到（stats.nba.com 可能被擋）。請先確認部署環境可連線 stats 或稍後重整。")
    st.stop()

nba_now = datetime.now(us_east_tz)
dates = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime("%m/%d") for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        date_et = dates[i]
        date_yyyy_mm_dd = date_et.strftime("%Y-%m-%d")

        games = get_live_scoreboard(date_yyyy_mm_dd)
        if not games:
            st.info("📅 目前抓不到賽程（Live Scoreboard 也可能暫時無資料/維護）。")
            continue

        injury_df = get_official_injury_report(date_yyyy_mm_dd)

        st.subheader("💰 當日賠率批次輸入（計算 Edge）")
        with st.expander("展開輸入當前賠率", expanded=True):
            input_odds = {}
            cols = st.columns(3)
            for idx, g in enumerate(games):
                with cols[idx % 3]:
                    st.write(f"**{g['label']}**")
                    oh = st.number_input(f"🏠 {TEAM_NAME_CH.get(g['home_abbr'], g['home_abbr'])}", value=1.75, step=0.01, key=f"oh_{i}_{idx}")
                    oa = st.number_input(f"✈️ {TEAM_NAME_CH.get(g['away_abbr'], g['away_abbr'])}", value=1.75, step=0.01, key=f"oa_{i}_{idx}")
                    input_odds[idx] = (oh, oa)

        analysis = []
        for idx, g in enumerate(games):
            if g["home_id"] is None or g["away_id"] is None:
                continue

            h_inj, h_det = injury_impact(g["home_abbr"], injury_df, player_ppg_db)
            a_inj, a_det = injury_impact(g["away_abbr"], injury_df, player_ppg_db)

            base_p, final_p, margin, net_diff = predict_home_win_prob(
                g["home_id"], g["away_id"], team_adv_map,
                home_inj_pen=h_inj, away_inj_pen=a_inj
            )

            oh, oa = input_odds[idx]
            imp_h = (1/oh) / ((1/oh) + (1/oa)) * 100
            imp_a = (1/oa) / ((1/oh) + (1/oa)) * 100
            edge_h = final_p - imp_h
            edge_a = (100 - final_p) - imp_a

            analysis.append({
                "label": g["label"],
                "h_abbr": g["home_abbr"], "a_abbr": g["away_abbr"],
                "h_id": g["home_id"], "a_id": g["away_id"],
                "h_ch": TEAM_NAME_CH.get(g["home_abbr"], g["home_abbr"]),
                "a_ch": TEAM_NAME_CH.get(g["away_abbr"], g["away_abbr"]),
                "base_p": base_p,
                "final_p": final_p,
                "margin": margin,
                "net_diff": net_diff,
                "h_inj": h_inj, "a_inj": a_inj,
                "h_det": h_det, "a_det": a_det,
                "odds_h": oh, "odds_a": oa,
                "edge_h": edge_h, "edge_a": edge_a
            })

        if not analysis:
            st.info("📅 今日沒有可分析的場次（資料不足）")
            continue

        st.divider()
        st.subheader("🔥 AI 推薦串關最優三場（NetRtg + 主場 + 官方傷病修正）")

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

        st.divider()
        sel = st.selectbox("🔍 選擇場次查看詳細", [d["label"] for d in analysis], key=f"sel_{i}")
        curr = next(d for d in analysis if d["label"] == sel)

        st.markdown(f"### 🏟️ {sel}")
        m1, m2, m3 = st.columns(3)
        m1.metric(curr["h_ch"], f"{curr['final_p']:.1f}%", f"預測分差: {curr['margin']:+.1f}")
        m2.metric(curr["a_ch"], f"{100-curr['final_p']:.1f}%", f"預測分差: {-curr['margin']:+.1f}")
        m3.metric("AI 建議贏家", curr["h_ch"] if curr["final_p"] >= 50 else curr["a_ch"])

        st.subheader("📌 Baseline vs 傷病修正")
        st.table(pd.DataFrame({
            "項目": ["Baseline 主隊勝率", "修正後主隊勝率", "NetRtg 差（主-客）", "主隊傷病影響", "客隊傷病影響"],
            "數值": [f"{curr['base_p']:.1f}%", f"{curr['final_p']:.1f}%", f"{curr['net_diff']:+.1f}", f"-{curr['h_inj']:.1f}", f"-{curr['a_inj']:.1f}"]
        }))

        st.subheader("🚑 官方傷病（NBA Injury Report PDF）")
        ic1, ic2 = st.columns(2)
        with ic1:
            st.write(f"**{curr['h_ch']}：影響合計 -{curr['h_inj']:.1f}**")
            if curr["h_det"]:
                for x in curr["h_det"]:
                    st.write(x)
            else:
                st.success("官方 injury report 未列出或無顯著影響")
        with ic2:
            st.write(f"**{curr['a_ch']}：影響合計 -{curr['a_inj']:.1f}**")
            if curr["a_det"]:
                for x in curr["a_det"]:
                    st.write(x)
            else:
                st.success("官方 injury report 未列出或無顯著影響")

st.sidebar.caption(f"Season：{SEASON}")
st.sidebar.caption(f"🕒 更新時間：{datetime.now(tw_tz).strftime('%Y-%m-%d %H:%M:%S')}")
st.sidebar.info("📌 賽程來源：nba_api.live ScoreBoard（通常可用）")
st.sidebar.info("📌 傷病來源：NBA 官方 Injury Report PDF（official.nba.com）")
