import streamlit as st
from nba_api.stats.endpoints import scoreboardv2, leaguedashplayerstats, teamgamelog
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, re, unicodedata, time, math
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# =========================================================
# 0) 版本 / 說明
# =========================================================
APP_VERSION = "v16.2 (Calibrated + Backtest Fit)"

# 你要求的「再更硬核」校準（回測校準）這版做法：
# - 新增「校準/回測」面板：可上傳你自己的歷史資料 CSV
# - 用歷史資料自動 fit：
#   (1) sigma：用 grid search 找到讓 NLL 最小的 sigma（常態CDF模型）
#   (2) Platt scaling（可選）：對 raw probability 做二次校準，降低過度自信
#
# 注意：你目前即時系統沒有「歷史市場盤口」與「歷史賽果」的可靠來源，
# 所以最穩健的方法是你提供/累積自己的歷史樣本（f_edge 與 cover 結果）。
# 這版 code 已把 pipeline 做好：你只要匯出 CSV 上傳即可自動校準。

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
# 2) 工具：名字正規化 + endpoint 安全抓取
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
# 3) 機率映射（校準版）+ 回測擬合（sigma + Platt scaling）
# =========================================================
PROB_FLOOR = 0.12
PROB_CEIL  = 0.88

SIGMA_BASE = 12.0
SIGMA_NO_INJ = 15.0
SIGMA_PER_Q = 0.9
SIGMA_PER_OUT = 0.4
SIGMA_CAP = 19.0

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

def sigmoid(x: float) -> float:
    # 避免 overflow
    if x >= 20:
        return 1.0
    if x <= -20:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))

def logit(p: float) -> float:
    p = clamp(p, 1e-6, 1 - 1e-6)
    return math.log(p / (1 - p))

def calc_cover_prob_raw(f_edge: float, sigma: float) -> float:
    """raw 機率：常態CDF，並做硬性截斷"""
    if sigma <= 0:
        sigma = SIGMA_BASE
    p = norm_cdf(f_edge / sigma)
    return clamp(p, PROB_FLOOR, PROB_CEIL)

def apply_platt(p_raw: float, a: float, b: float) -> float:
    """
    Platt scaling：p_cal = sigmoid(a + b*logit(p_raw))
    - 若 a=0,b=1 → 不改
    """
    z = a + b * logit(p_raw)
    p = sigmoid(z)
    return clamp(p, PROB_FLOOR, PROB_CEIL)

def nll_bernoulli(p: float, y: float) -> float:
    p = clamp(p, 1e-9, 1 - 1e-9)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))

def fit_sigma_by_grid(df: pd.DataFrame, fcol="f_edge", ycol="cover",
                      sigma_min=7.0, sigma_max=20.0, sigma_step=0.25) -> dict:
    """
    用歷史資料找最好的 sigma（最小化 NLL）
    需要 df[f_edge], df[cover]，cover ∈ {0,1}
    """
    if df.empty or fcol not in df.columns or ycol not in df.columns:
        return {"ok": False, "sigma": SIGMA_BASE, "n": 0, "best_nll": None}

    x = pd.to_numeric(df[fcol], errors="coerce")
    y = pd.to_numeric(df[ycol], errors="coerce")
    m = x.notna() & y.notna()
    x = x[m].tolist()
    y = y[m].tolist()

    if len(x) < 50:
        # 樣本太少不建議 fit
        return {"ok": False, "sigma": SIGMA_BASE, "n": len(x), "best_nll": None}

    best_sigma = SIGMA_BASE
    best_nll = float("inf")

    s = sigma_min
    while s <= sigma_max + 1e-9:
        total = 0.0
        for xi, yi in zip(x, y):
            p = calc_cover_prob_raw(float(xi), float(s))
            total += nll_bernoulli(p, float(yi))
        avg = total / len(x)
        if avg < best_nll:
            best_nll = avg
            best_sigma = s
        s += sigma_step

    return {"ok": True, "sigma": float(best_sigma), "n": len(x), "best_nll": float(best_nll)}

def fit_platt(df: pd.DataFrame, sigma: float, fcol="f_edge", ycol="cover",
              iters=25, lr=0.4, l2=1e-3) -> dict:
    """
    以 raw prob = CDF(f_edge/sigma) 當 base，fit a,b 讓 NLL 最小
    使用簡單梯度下降（穩定、無 sklearn 依賴）
    """
    if df.empty or fcol not in df.columns or ycol not in df.columns:
        return {"ok": False, "a": 0.0, "b": 1.0, "n": 0, "nll": None}

    x = pd.to_numeric(df[fcol], errors="coerce")
    y = pd.to_numeric(df[ycol], errors="coerce")
    m = x.notna() & y.notna()
    x = x[m].tolist()
    y = y[m].tolist()

    if len(x) < 200:
        # Platt 至少要多一點樣本，否則容易 overfit
        return {"ok": False, "a": 0.0, "b": 1.0, "n": len(x), "nll": None}

    a, b = 0.0, 1.0

    def compute_nll(a_, b_) -> float:
        total = 0.0
        for xi, yi in zip(x, y):
            p_raw = calc_cover_prob_raw(float(xi), sigma)
            p_cal = apply_platt(p_raw, a_, b_)
            total += nll_bernoulli(p_cal, float(yi))
        total = total / len(x)
        # L2 regularization 避免 b 暴衝
        total += l2 * (a_ * a_ + (b_ - 1.0) * (b_ - 1.0))
        return total

    for _ in range(iters):
        # numerical gradient（簡單可靠）
        eps = 1e-4
        base = compute_nll(a, b)
        da = (compute_nll(a + eps, b) - compute_nll(a - eps, b)) / (2 * eps)
        db = (compute_nll(a, b + eps) - compute_nll(a, b - eps)) / (2 * eps)

        a = a - lr * da
        b = b - lr * db

        # 合理限制（避免奇怪校準）
        a = clamp(a, -4.0, 4.0)
        b = clamp(b, 0.2, 3.0)

        # 若改善很小可提早停
        new = compute_nll(a, b)
        if abs(base - new) < 1e-5:
            break

    final_nll = compute_nll(a, b)
    return {"ok": True, "a": float(a), "b": float(b), "n": len(x), "nll": float(final_nll)}

# =========================================================
# 4) 賽程抓取
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
# 5) 球員資料（全聯盟）— cache
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
# 6) 傷病報告（ESPN）— cache（含 IS_Q）
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
                    }
                )
    except Exception:
        pass

    return pd.DataFrame(inj_list)

# =========================================================
# 7) 隊伍 Context — cache
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
# 8) Odds API（Pinnacle）抓盤口/賠率 — cache
# =========================================================
@st.cache_data(ttl=900)
def get_pinnacle_odds_for_date(game_date_us: str) -> dict:
    api_key = None
    try:
        api_key = st.secrets.get("ODDS_API_KEY", None)
    except Exception:
        api_key = None

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

# =========================================================
# 9) UI 初始化 + 校準/回測面板
# =========================================================
st.set_page_config(page_title=f"NBA Edge {APP_VERSION}", layout="wide")

h1, h2 = st.columns([0.78, 0.22])
with h1:
    now_tw_str = datetime.now(tw_tz).strftime("%m/%d %H:%M")
    st.title("🏀 NBA Edge 數據預測系統")
    st.caption(f"版本：{APP_VERSION}｜台灣現在時間：{now_tw_str}")
with h2:
    if st.button("🔄 強制更新（傷病/盤口/數據）"):
        st.cache_data.clear()
        st.rerun()
    with st.popover("💡 判讀指南（含回測校準）"):
        st.markdown(
            "**點數優勢**：模型預測分差與盤口的差距（點數）。\n\n"
            "**過盤機率（校準）**：先用常態 CDF(f_edge/sigma) 估，再用 Platt scaling（可選）校準。\n\n"
            "**盤口優勢**：過盤機率（校準） - 損益兩平機率。\n\n"
            "**期望報酬**：以過盤機率（校準）估算的長期期望。\n\n"
            "**回測資料格式（CSV）**：\n"
            "- 必要欄位：`f_edge`、`cover`\n"
            "- `cover`：主隊是否過盤（1=過盤, 0=沒過）\n"
            "- `f_edge`：你當時算的（base_diff + 主隊盤口）\n\n"
            "**Top picks 規則不變**：候選池只用 Pinnacle 真盤；排序用你手動輸入的盤口/賠率重算。"
        )

# 側邊欄：校準/回測
with st.sidebar:
    st.header("🧪 校準 / 回測")
    st.caption("上傳你的歷史樣本 CSV，自動 fit sigma + Platt scaling（可選）")
    calib_file = st.file_uploader("上傳回測 CSV（含 f_edge, cover）", type=["csv"])

    use_sigma_fit = st.toggle("啟用 sigma 擬合", value=True)
    use_platt_fit = st.toggle("啟用 Platt scaling（需要較大樣本）", value=True)

    st.divider()
    st.subheader("⚙️ 校準參數（預設/上限）")
    sigma_min = st.number_input("sigma 搜尋下限", 5.0, 30.0, 7.0, 0.5)
    sigma_max = st.number_input("sigma 搜尋上限", 5.0, 40.0, 20.0, 0.5)
    sigma_step = st.number_input("sigma 步長", 0.05, 2.0, 0.25, 0.05)

    st.caption("提醒：若樣本 < 50，不做 sigma fit；樣本 < 200，不做 Platt fit（避免過度擬合）。")

# 讀取校準資料 & 擬合
fit_sigma = {"ok": False, "sigma": SIGMA_BASE, "n": 0, "best_nll": None}
fit_pl = {"ok": False, "a": 0.0, "b": 1.0, "n": 0, "nll": None}

calib_df = pd.DataFrame()
if calib_file is not None:
    try:
        calib_df = pd.read_csv(calib_file)
    except Exception:
        calib_df = pd.DataFrame()

if (not calib_df.empty) and use_sigma_fit:
    fit_sigma = fit_sigma_by_grid(
        calib_df, fcol="f_edge", ycol="cover",
        sigma_min=float(sigma_min), sigma_max=float(sigma_max), sigma_step=float(sigma_step)
    )

sigma_fit_value = fit_sigma["sigma"] if fit_sigma["ok"] else SIGMA_BASE

if (not calib_df.empty) and use_platt_fit:
    fit_pl = fit_platt(calib_df, sigma=sigma_fit_value, fcol="f_edge", ycol="cover")

# 顯示校準結果
with st.sidebar:
    st.subheader("📌 校準結果")
    if calib_file is None:
        st.info("尚未上傳回測資料：使用預設 sigma 與不套用 Platt（a=0,b=1）。")
    else:
        if fit_sigma["ok"]:
            st.success(f"sigma 擬合成功：{fit_sigma['sigma']:.2f}（n={fit_sigma['n']}，NLL={fit_sigma['best_nll']:.4f}）")
        else:
            st.warning(f"sigma 未擬合（n={fit_sigma['n']}）：改用預設 {SIGMA_BASE:.1f}")

        if fit_pl["ok"]:
            st.success(f"Platt 擬合成功：a={fit_pl['a']:.3f}, b={fit_pl['b']:.3f}（n={fit_pl['n']}，NLL={fit_pl['nll']:.4f}）")
        else:
            st.warning(f"Platt 未擬合（n={fit_pl['n']}）：不套用（a=0,b=1）")

# =========================================================
# 10) 抓今日資料
# =========================================================
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
    st.warning("⚠️ ESPN 傷病名單抓不到：系統將自動更保守（sigma↑）。")

pinnacle_map = get_pinnacle_odds_for_date(target_date_us)

# =========================================================
# 11) 主計算：每場 base_diff（保留你的核心公式）
# =========================================================
all_games_data = []

for _, row in sb_filtered.iterrows():
    h_id, a_id = row["HOME_TEAM_ID"], row["VISITOR_TEAM_ID"]
    h_abbr, a_abbr = ID_MAP.get(h_id, str(h_id)), ID_MAP.get(a_id, str(a_id))

    def build_pkg(tid: int, abbr: str):
        ctx = ctx_db.get(tid, {"b2b": False, "recent_w": 0.5})

        t_inj = inj_db[inj_db["球隊"] == abbr] if not inj_db.empty else pd.DataFrame()
        out_list = t_inj[t_inj.get("IS_OUT", False)]["NORM"].tolist() if not t_inj.empty else []

        if not ps_db.empty and "TEAM_ID" in ps_db.columns and "NORM" in ps_db.columns:
            active = (
                ps_db[(ps_db["TEAM_ID"] == tid) & (~ps_db["NORM"].isin(out_list))]
                .sort_values("IMPACT", ascending=False)
                .copy()
            )
        else:
            active = pd.DataFrame()

        n_out = int(t_inj["IS_OUT"].sum()) if (not t_inj.empty and "IS_OUT" in t_inj.columns) else 0
        n_q   = int(t_inj["IS_Q"].sum()) if (not t_inj.empty and "IS_Q" in t_inj.columns) else 0

        return {
            "pts": float(active["PTS"].sum()) if not active.empty and "PTS" in active.columns else 0.0,
            "impact": float(active["IMPACT"].mean()) if not active.empty and "IMPACT" in active.columns else 0.0,
            "df": active,
            "inj": t_inj,
            "b2b": bool(ctx["b2b"]),
            "recent_w": float(ctx["recent_w"]),
            "n_out": n_out,
            "n_q": n_q,
        }

    h_p, a_p = build_pkg(h_id, h_abbr), build_pkg(a_id, a_abbr)

    b2b_v = (-2.5 if h_p["b2b"] else 0) - (-2.5 if a_p["b2b"] else 0)
    recent_v = (h_p["recent_w"] - a_p["recent_w"]) * 5

    base_diff = (h_p["pts"] - a_p["pts"]) * 0.09 + (h_p["impact"] - a_p["impact"]) * 3.8 + 2.5 + b2b_v + recent_v

    game_id = f"{a_abbr}_{h_abbr}_{target_date_us.replace('/','')}"
    a_cn = TEAM_NAME_CH.get(a_abbr, a_abbr)
    h_cn = TEAM_NAME_CH.get(h_abbr, h_abbr)

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
# 12) 挑場規則：候選池=真盤；排序=你手動輸入
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
    sp_default = g["pin_home_sp"]
    oh_default = g["pin_home_od"]
    oa_default = g["pin_away_od"]

    sp = safe_float(st.session_state.get(f"sp_{gid}", sp_default), sp_default)
    oh = safe_float(st.session_state.get(f"oh_{gid}", oh_default), oh_default)
    oa = safe_float(st.session_state.get(f"oa_{gid}", oa_default), oa_default)

    manual = (abs(sp - sp_default) > 1e-9) or (abs(oh - oh_default) > 1e-9) or (abs(oa - oa_default) > 1e-9)

    if manual:
        src = "手動（運彩）✍️"
    elif g["pin_ok"]:
        src = "Pinnacle ✅"
    else:
        src = "Fallback ⚠️"

    return float(sp), float(oh), float(oa), src, manual

def compute_sigma_for_game(g) -> float:
    """
    比賽層級 sigma：用「回測擬合 sigma」當 base，
    再依資訊不確定性（inj empty / Q / OUT）加成。
    """
    base_sigma = sigma_fit_value if (fit_sigma["ok"] and use_sigma_fit) else SIGMA_BASE

    if inj_db.empty:
        sigma = max(SIGMA_NO_INJ, base_sigma)
        return float(min(SIGMA_CAP, sigma))

    h = g["h_pkg"]
    a = g["a_pkg"]
    n_out = int(h.get("n_out", 0) + a.get("n_out", 0))
    n_q   = int(h.get("n_q", 0) + a.get("n_q", 0))

    sigma = float(base_sigma) + (n_q * SIGMA_PER_Q) + (n_out * SIGMA_PER_OUT)
    sigma = min(SIGMA_CAP, sigma)
    return float(sigma)

def compute_metrics(g, home_spread_input, home_odds, away_odds):
    # f_edge：模型點差（home vs away） + 主隊盤口（主讓負、主受讓正）
    f_edge = g["base_diff"] + home_spread_input

    sigma = compute_sigma_for_game(g)
    p_raw = calc_cover_prob_raw(f_edge, sigma)

    # Platt scaling（若成功擬合且啟用）
    if fit_pl["ok"] and use_platt_fit:
        cover_prob = apply_platt(p_raw, fit_pl["a"], fit_pl["b"])
        cal_tag = "Platt"
    else:
        cover_prob = p_raw
        cal_tag = "RawCDF"

    pick_team = g["h_cn"] if f_edge > 0 else g["a_cn"]
    odds = home_odds if f_edge > 0 else away_odds

    implied_prob = 1.0 / odds if odds and odds > 0 else 1.0
    edge_value = cover_prob - implied_prob
    ev = (cover_prob * odds) - 1

    return {
        "f_edge": float(f_edge),
        "edge_points": float(abs(f_edge)),
        "sigma": float(sigma),
        "cal_tag": cal_tag,
        "cover_prob": float(cover_prob),
        "p_raw": float(p_raw),
        "implied_prob": float(implied_prob),
        "edge_value": float(edge_value),
        "ev": float(ev),
        "pick_team": pick_team,
        "odds_used": float(odds),
    }

# =========================================================
# 13) 🔥 今日推薦
# =========================================================
pool_games = all_games_data[:MAX_GAMES_FOR_PICK]

pick_pool = []
for g in pool_games:
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
                    f"過盤機率（{item['cal_tag']}）：**{item['cover_prob']*100:.1f}%** | "
                    f"損益兩平：**{item['implied_prob']*100:.1f}%**"
                )
                st.metric("盤口優勢", f"{item['edge_value']*100:+.1f}%")
                st.write(
                    f"主隊盤口：**{item['home_spread_input']:+.1f}** | "
                    f"主賠：**{item['home_odds']:.2f}** | 客賠：**{item['away_odds']:.2f}**"
                )
                st.write(
                    f"點數優勢：**{item['edge_points']:.1f}** | "
                    f"sigma：**{item['sigma']:.2f}** | "
                    f"raw：**{item['p_raw']*100:.1f}%** | "
                    f"期望報酬：**{item['ev']*100:+.1f}%**"
                )

st.divider()

# =========================================================
# 14) 🎯 全部場次與實時計算
# =========================================================
st.header("🎯 全部場次與實時計算")

for i in range(0, len(all_games_data), 3):
    cols = st.columns(3)
    for j, g in enumerate(all_games_data[i : i + 3]):
        with cols[j]:
            with st.container(border=True):
                st.subheader(g["label"])
                gid = g["game_id"]

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

                manual = (abs(float(u_sp) - sp_default) > 1e-9) or (abs(float(u_oh) - oh_default) > 1e-9) or (abs(float(u_oa) - oa_default) > 1e-9)
                if manual:
                    src = "手動（運彩）✍️"
                elif g["pin_ok"]:
                    src = "Pinnacle ✅"
                else:
                    src = "Fallback ⚠️"

                m = compute_metrics(g, float(u_sp), float(u_oh), float(u_oa))

                st.caption(f"盤口來源：{src}｜機率模式：{m['cal_tag']}（raw={m['p_raw']*100:.1f}%）")
                st.write(
                    f"過盤機率：**{m['cover_prob']*100:.1f}%** | "
                    f"點數優勢：**{m['edge_points']:.1f}** | "
                    f"sigma：**{m['sigma']:.2f}**"
                )
                st.write(f"盤口優勢：**{m['edge_value']*100:+.1f}%** | 期望報酬：**{m['ev']*100:+.1f}%**")

                if g["pin_ok"] and m["edge_value"] > EDGE_THRESHOLD:
                    st.success(f"🔥 符合挑場門檻（真盤候選 + 盤口優勢 > 5%）：{m['pick_team']}")
                else:
                    st.info(f"建議：{m['pick_team']}")

# =========================================================
# 15) 🔍 深度查詢
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

    # 顯示該場的 sigma/校準狀態
    sigma_dbg = compute_sigma_for_game(curr)
    p_dbg = calc_cover_prob_raw(curr["base_diff"] + curr["pin_home_sp"], sigma_dbg)
    if fit_pl["ok"] and use_platt_fit:
        p_dbg2 = apply_platt(p_dbg, fit_pl["a"], fit_pl["b"])
        st.caption(f"（Debug）sigma={sigma_dbg:.2f}｜raw={p_dbg*100:.1f}%｜platt={p_dbg2*100:.1f}%")
    else:
        st.caption(f"（Debug）sigma={sigma_dbg:.2f}｜raw={p_dbg*100:.1f}%（未套用 Platt）")

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
                show_inj_cols = [c for c in ["球員", "狀態", "原因"] if c in pkg["inj"].columns]
                st.dataframe(pkg["inj"][show_inj_cols], hide_index=True)
                st.caption(f"不確定性：OUT={pkg.get('n_out',0)} / Q={pkg.get('n_q',0)}（Q 會讓 sigma ↑ 更保守）")
            else:
                st.write("✅ 無傷病報告")

st.caption(
    f"（基礎參數：SIGMA_BASE={SIGMA_BASE:.1f}；inj缺失→sigma≥{SIGMA_NO_INJ:.1f}；"
    f"Q 每人 +{SIGMA_PER_Q:.1f}；OUT 每人 +{SIGMA_PER_OUT:.1f}；"
    f"sigma cap={SIGMA_CAP:.1f}；硬性截斷 {int(PROB_FLOOR*100)}%~{int(PROB_CEIL*100)}%）"
)

# =========================================================
# 16) （可選）回測資料模板下載提示（不生成檔案，直接示範欄位）
# =========================================================
with st.expander("📄 回測 CSV 欄位模板（你可以照這個格式累積資料）"):
    st.markdown(
        "- `date`：比賽日期（可選）\n"
        "- `matchup`：對戰（可選）\n"
        "- `f_edge`：你當天算出來的 `base_diff + 主隊盤口`\n"
        "- `cover`：主隊是否過盤（1=過盤, 0=沒過）\n"
        "\n"
        "最少只需要 `f_edge, cover` 兩欄就能校準。\n"
        "\n"
        "建議做法：每次你下注/觀察時，把該場的 f_edge 記下來，等賽果出來填 cover，累積到 200+ 場再開 Platt。"
    )
