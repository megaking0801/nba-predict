import streamlit as st
from nba_api.stats.endpoints import scoreboardv2, leaguedashplayerstats, teamgamelog
from nba_api.stats.static import teams
import pandas as pd
import pytz, warnings, requests, re, unicodedata, time
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

    # 避免板凳路人影響團隊火力估計
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
@st.cache_data(ttl=3600)
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
                if len(cols) < 3:
                    continue

                raw_player = cols[0].get_text(" ", strip=True)
                raw_player = re.sub(r"\s+(PG|SG|SF|PF|C|G|F)\s*$", "", raw_player, flags=re.I).strip()

                raw_status = cols[2].get_text(" ", strip=True).lower()
                raw_reason = cols[-1].get_text(" ", strip=True) if len(cols) >= 4 else "無"
                text_blob = (raw_status + " " + raw_reason).lower()

                is_out = any(w in text_blob for w in ["out", "surgery", "suspended", "season"])
                is_q = any(w in text_blob for w in ["questionable", "gtd", "day-to-day", "doubtful", "probable"])

                if is_out:
                    status_cn = "❌ [確定缺陣]"
                elif is_q:
                    status_cn = "📋 [觀察名單]"
                else:
                    status_cn = "✅ [預計出賽]"

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
#    - 真 B2B：本場日期前一天是否有比賽
#    - 近五場勝率：本場日期之前最近 5 場
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
# 7) UI 初始化（保留原本配置）
# =========================================================
st.set_page_config(page_title="NBA Edge v16.0", layout="wide")

h1, h2 = st.columns([0.8, 0.2])
with h1:
    now_tw_str = datetime.now(tw_tz).strftime("%m/%d %H:%M")
    st.title("🏀 NBA Edge 數據預測系統")
    st.caption(f"台灣現在時間：{now_tw_str}")
with h2:
    with st.popover("💡 判讀指南"):
        st.markdown("**Edge**: 模型預測分差與盤口的差距。\n\n**EV**: >10% 為極佳機會。")

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
# 8) 主計算：建立每場 pkg + base_diff（保留你的核心公式）
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

    # 穩定唯一 ID（避免 widget key 炸掉）
    game_id = f"{a_abbr}_{h_abbr}_{target_date_us.replace('/','')}"
    a_cn = TEAM_NAME_CH.get(a_abbr, a_abbr)
    h_cn = TEAM_NAME_CH.get(h_abbr, h_abbr)

    all_games_data.append(
        {
            "game_id": game_id,
            "label": f"{a_cn}(客) @ {h_cn}(主)",
            "base_diff": float(base_diff),
            "h_pkg": h_p,
            "a_pkg": a_p,
            "h_cn": h_cn,
            "a_cn": a_cn,
        }
    )


# =========================================================
# 9) 🔥 今日最能買（至多三場）— 依你新挑場規則（保留原 UI 區塊位置）
#    規則：
#    1) 算 edge_value = cover_prob - implied_prob
#    2) 只保留 edge_value > 5%
#    3) 從中挑 cover_prob 高的前 1~3 場（最多三場）
#    ※ 若只有 1 場符合，就只顯示 1 場（不硬湊）
# =========================================================
EDGE_THRESHOLD = 0.05
MAX_PICKS = 3
MAX_GAMES_FOR_PICK = 10  # 你指定「在 10 場裡」

def calc_cover_prob(edge_points: float) -> float:
    # 你原本的映射（保留）
    return 1 / (1 + 10 ** (-abs(edge_points) / 11))

def get_market_inputs_for_game(g):
    # 如果使用者已在下方輸入盤口/賠率，Streamlit rerun 後會留在 session_state
    gid = g["game_id"]
    sp = st.session_state.get(f"sp_{gid}", 0.0)
    oh = st.session_state.get(f"oh_{gid}", 1.90)
    oa = st.session_state.get(f"oa_{gid}", 1.90)
    return float(sp), float(oh), float(oa)

# 先用「目前已輸入/預設」的盤口與賠率，計算挑場（不改 UI，只改推薦邏輯）
pick_pool = []
for g in all_games_data[:MAX_GAMES_FOR_PICK]:
    u_sp, u_oh, u_oa = get_market_inputs_for_game(g)

    f_edge = g["base_diff"] + u_sp
    cover_prob = calc_cover_prob(f_edge)

    pick_side = g["h_cn"] if f_edge > 0 else g["a_cn"]
    odds = u_oh if f_edge > 0 else u_oa
    implied_prob = 1.0 / odds if odds and odds > 0 else 1.0

    edge_value = cover_prob - implied_prob  # >0.05 才符合規則

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
    st.info("依挑場規則：目前前 10 場中沒有任何一場 edge_value > 5%（建議不買、不硬湊）。")
else:
    if len(picks) == 1:
        st.success("🎯 今日只有 1 場符合 edge_value > 5%：建議只買單場（或分注單場），不要硬湊串關。")
    else:
        st.success(f"🎯 今日最能買：已依規則挑出 {len(picks)} 場（最多三場）。")

    cols = st.columns(len(picks))
    for idx, item in enumerate(picks):
        g = item["g"]
        with cols[idx]:
            with st.container(border=True):
                st.subheader(f"Pick {idx+1}")
                st.write(f"**{g['label']}**")
                st.success(f"首選：{item['pick_side']}")
                st.write(
                    f"cover_prob: **{item['cover_prob']*100:.1f}%** | "
                    f"implied: **{item['implied_prob']*100:.1f}%**"
                )
                st.metric("edge_value", f"{item['edge_value']*100:+.1f}%")
                st.write(f"主隊盤口: **{item['home_spread_input']}** | 賠率: **{item['odds']:.2f}**")
                st.write(f"Edge(點數): **{item['edge_points']:.1f}**")

st.divider()


# =========================================================
# 10) 🎯 全部場次與實時計算（保留原 UI；主隊盤口輸入規則）
# =========================================================
st.header("🎯 全部場次與實時計算")

for i in range(0, len(all_games_data), 3):
    cols = st.columns(3)
    for j, g in enumerate(all_games_data[i : i + 3]):
        with cols[j]:
            with st.container(border=True):
                st.subheader(g["label"])

                gid = g["game_id"]

                # ✅ 只輸入「主隊盤口」：
                # 主隊讓分：輸入負數（例：主 -5.5 → -5.5）
                # 主隊受讓：輸入正數（例：主 +3.5 → +3.5）
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

                f_edge = g["base_diff"] + u_sp
                prob = calc_cover_prob(f_edge) * 100
                rec = g["h_cn"] if f_edge > 0 else g["a_cn"]
                odds = u_oh if f_edge > 0 else u_oa
                ev = (prob / 100 * odds) - 1

                # 新增：edge_value（%）＝cover_prob - implied_prob（與上方挑場一致）
                cover_prob = prob / 100.0
                implied_prob = 1.0 / odds if odds and odds > 0 else 1.0
                edge_value = cover_prob - implied_prob

                st.write(f"勝率: **{prob:.1f}%** | Edge(點數): **{abs(f_edge):.1f}**")
                st.write(f"edge_value: **{edge_value*100:+.1f}%** | EV: **{ev*100:+.1f}%**")

                if edge_value > EDGE_THRESHOLD:
                    st.success(f"🔥 符合挑場門檻（edge_value>5%）：{rec}")
                else:
                    st.info(f"建議：{rec}")


# =========================================================
# 11) 🔍 深度查詢（保留原 UI）
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
