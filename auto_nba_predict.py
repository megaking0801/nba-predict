import streamlit as st
import pandas as pd
import numpy as np
import pytz, warnings, requests, re, unicodedata
from datetime import datetime, timedelta

from nba_api.stats.static import teams
from nba_api.live.nba.endpoints import scoreboard as live_scoreboard

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

st.set_page_config(page_title="NBA 數據專家 v9.4（無 stats/無 standings）", layout="wide")
st.title("🏀 NBA 數據專家 v9.4（Live 賽程 + 近況回溯 + 官方 Injury Report）")

# -------------------------
# utils
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
    fullname_to_abbr["LA Clippers"] = "LAC"
    fullname_to_abbr["LA Lakers"] = "LAL"
    fullname_to_abbr["Los Angeles Clippers"] = "LAC"
    fullname_to_abbr["Los Angeles Lakers"] = "LAL"
    return id_to_abbr, abbr_to_id, fullname_to_abbr

ID_TO_ABBR, ABBR_TO_ID, FULLNAME_TO_ABBR = build_team_maps()

# -------------------------
# 1) live scoreboard (today)
# -------------------------
@st.cache_data(ttl=120, show_spinner=False)
def get_live_scoreboard_games(date_yyyy_mm_dd: str) -> list[dict]:
    """
    games list: {home_abbr, away_abbr, label, statusText, home_score, away_score}
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
            "label": f"{TEAM_NAME_CH.get(a_abbr, a_abbr)} @ {TEAM_NAME_CH.get(h_abbr, h_abbr)}",
            "statusText": g.get("gameStatusText", ""),
            "home_score": float(h.get("score") or 0),
            "away_score": float(a.get("score") or 0),
            "gameStatus": int(g.get("gameStatus") or 0),  # 1 scheduled, 2 live, 3 final (常見)
        })
    return out

# -------------------------
# 2) build "recent form" team strength from last X days finals
# -------------------------
@st.cache_data(ttl=600, show_spinner=True)
def build_recent_form(days_back: int = 21) -> dict:
    """
    回溯近 days_back 天的 live scoreboard，把 final 比賽抓下來
    給每隊：
      - win_pct_recent
      - avg_margin_recent（平均分差，贏為正、輸為負）
      - games
    """
    today = datetime.now(us_east_tz).date()
    rows = []

    for d in range(1, days_back + 1):
        dt = (today - timedelta(days=d)).strftime("%Y-%m-%d")
        try:
            sb = live_scoreboard.ScoreBoard(game_date=dt).get_dict()
            games = sb.get("scoreboard", {}).get("games", [])
        except Exception:
            continue

        for g in games:
            if int(g.get("gameStatus") or 0) != 3:
                continue  # only finals

            h = g.get("homeTeam", {})
            a = g.get("awayTeam", {})
            h_abbr = h.get("teamTricode")
            a_abbr = a.get("teamTricode")
            if not h_abbr or not a_abbr:
                continue

            hs = float(h.get("score") or 0)
            as_ = float(a.get("score") or 0)
            if hs == 0 and as_ == 0:
                continue

            # home record
            rows.append({"team": h_abbr, "win": 1 if hs > as_ else 0, "margin": hs - as_})
            # away record
            rows.append({"team": a_abbr, "win": 1 if as_ > hs else 0, "margin": as_ - hs})

    if not rows:
        return {}

    df = pd.DataFrame(rows)
    agg = df.groupby("team").agg(
        games=("win", "count"),
        win_pct=("win", "mean"),
        avg_margin=("margin", "mean")
    ).reset_index()

    out = {}
    for _, r in agg.iterrows():
        out[r["team"]] = {
            "games": int(r["games"]),
            "win_pct": float(r["win_pct"]),
            "avg_margin": float(r["avg_margin"])
        }
    return out

# -------------------------
# 3) official injury report pdf
# -------------------------
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

def injury_impact(team_abbr: str, injury_df: pd.DataFrame):
    """
    不抓球員 PPG，使用狀態固定扣分（百分點）
    """
    if injury_df.empty:
        return 0.0, []

    sub = injury_df[injury_df["TEAM_ABBR"] == team_abbr]
    if sub.empty:
        return 0.0, []

    status_points = {
        "Out": 3.0,
        "Doubtful": 2.0,
        "Questionable": 1.2,
        "Probable": 0.6,
        "Available": 0.0
    }

    total = 0.0
    details = []
    for _, r in sub.iterrows():
        player = str(r["PLAYER"])
        status = str(r["STATUS"])
        reason = str(r["REASON"])
        pen = float(status_points.get(status, 0.0))
        if pen <= 0:
            continue
        icon = "❌" if status in ["Out", "Doubtful"] else "⚠️"
        details.append(f"{icon} {player}：{status} 影響 -{pen:.1f}｜{reason}")
        total += pen

    total = min(18.0, total)
    return total, details

# -------------------------
# 4) win prob (recent form + home adv + injury)
# -------------------------
def predict_home_win_prob(home_abbr: str, away_abbr: str,
                          recent_map: dict,
                          home_inj: float, away_inj: float):
    """
    recent form baseline:
      - win_pct_recent diff
      - avg_margin_recent diff
    轉 z -> sigmoid
    若 recent_map 沒資料：回 50%
    """
    h = recent_map.get(home_abbr)
    a = recent_map.get(away_abbr)

    if not h or not a:
        base_p = 50.0
        final_p = clamp(50.0 - home_inj + away_inj, 5, 95)
        margin = 0.0 - (home_inj - away_inj) * 0.35
        return base_p, final_p, margin, None

    win_diff = float(h["win_pct"]) - float(a["win_pct"])
    mar_diff = float(h["avg_margin"]) - float(a["avg_margin"])

    home_adv_z = 0.12
    z = (win_diff * 2.2) + (mar_diff * 0.08) + home_adv_z

    base_p = sigmoid(z) * 100
    final_p = clamp(base_p - home_inj + away_inj, 5, 95)

    margin = (mar_diff * 0.65) - (home_inj - away_inj) * 0.35
    return base_p, final_p, margin, {"win_diff": win_diff, "mar_diff": mar_diff, "h_games": h["games"], "a_games": a["games"]}

# =========================
# UI
# =========================
recent_map = build_recent_form(days_back=21)
if not recent_map:
    st.warning("⚠️ 近況回溯資料抓不到（可能 live endpoint 暫時限流/維護）。勝率將回退到 50% + 傷病修正。")

nba_now = datetime.now(us_east_tz)
dates = [nba_now + timedelta(days=1), nba_now, nba_now - timedelta(days=1)]
tabs = st.tabs([d.astimezone(tw_tz).strftime("%m/%d") for d in dates])

for i, tab in enumerate(tabs):
    with tab:
        date_et = dates[i]
        date_yyyy_mm_dd = date_et.strftime("%Y-%m-%d")

        games = get_live_scoreboard_games(date_yyyy_mm_dd)
        if not games:
            st.info("📅 目前抓不到賽程（Live Scoreboard 暫時無資料/維護/該日無賽程）。")
            continue

        injury_df = get_official_injury_report(date_yyyy_mm_dd)

        st.subheader("💰 當日賠率批次輸入（計算 Edge）")
        with st.expander("展開輸入當前賠率", expanded=True):
            input_odds = {}
            cols = st.columns(3)
            for idx, g in enumerate(games):
                with cols[idx % 3]:
                    st.write(f"**{g['label']}**")
                    st.caption(g.get("statusText",""))
                    oh = st.number_input(f"🏠 {TEAM_NAME_CH.get(g['home_abbr'], g['home_abbr'])}", value=1.75, step=0.01, key=f"oh_{i}_{idx}")
                    oa = st.number_input(f"✈️ {TEAM_NAME_CH.get(g['away_abbr'], g['away_abbr'])}", value=1.75, step=0.01, key=f"oa_{i}_{idx}")
                    input_odds[idx] = (oh, oa)

        analysis = []
        for idx, g in enumerate(games):
            h_abbr, a_abbr = g["home_abbr"], g["away_abbr"]

            h_inj, h_det = injury_impact(h_abbr, injury_df)
            a_inj, a_det = injury_impact(a_abbr, injury_df)

            base_p, final_p, margin, feat = predict_home_win_prob(h_abbr, a_abbr, recent_map, h_inj, a_inj)

            oh, oa = input_odds[idx]
            imp_h = (1/oh) / ((1/oh) + (1/oa)) * 100
            imp_a = (1/oa) / ((1/oh) + (1/oa)) * 100
            edge_h = final_p - imp_h
            edge_a = (100 - final_p) - imp_a

            analysis.append({
                "label": g["label"],
                "h_abbr": h_abbr, "a_abbr": a_abbr,
                "h_ch": TEAM_NAME_CH.get(h_abbr, h_abbr),
                "a_ch": TEAM_NAME_CH.get(a_abbr, a_abbr),
                "base_p": base_p,
                "final_p": final_p,
                "margin": margin,
                "feat": feat,
                "h_inj": h_inj, "a_inj": a_inj,
                "h_det": h_det, "a_det": a_det,
                "odds_h": oh, "odds_a": oa,
                "edge_h": edge_h, "edge_a": edge_a
            })

        st.divider()
        st.subheader("🔥 推薦串關最優三場（近況回溯 + 主場 + 官方傷病）")

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

        st.subheader("📌 Baseline vs 修正（近況特徵）")
        if curr["feat"] is None:
            st.info("近況資料不足：baseline 以 50% 為基底（只做傷病修正）。")
        else:
            st.table(pd.DataFrame({
                "項目": ["Baseline 主隊勝率", "修正後主隊勝率", "Win% 差（主-客）", "AvgMargin 差（主-客）", "樣本場數（主/客）", "主隊傷病影響", "客隊傷病影響"],
                "數值": [
                    f"{curr['base_p']:.1f}%",
                    f"{curr['final_p']:.1f}%",
                    f"{curr['feat']['win_diff']:+.3f}",
                    f"{curr['feat']['mar_diff']:+.2f}",
                    f"{curr['feat']['h_games']}/{curr['feat']['a_games']}",
                    f"-{curr['h_inj']:.1f}",
                    f"-{curr['a_inj']:.1f}",
                ]
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

st.sidebar.caption(f"🕒 更新時間：{datetime.now(tw_tz).strftime('%Y-%m-%d %H:%M:%S')}")
st.sidebar.info("📌 賽程來源：nba_api.live ScoreBoard")
st.sidebar.info("📌 強弱來源：回溯近 21 天 finals（live scoreboard）")
st.sidebar.info("📌 傷病來源：NBA 官方 Injury Report PDF（official.nba.com）")
