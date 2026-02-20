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
# 2) 工具：更強的名字正規化 + endpoint 安全抓取（含簡單重試）
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
    """
    nba_api 偶發 timeout / rate-limit 時，簡單重試。
    """
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
    """
    先鎖定美東「今天」日期抓 scoreboard；
    若真的無賽程，再抓明天。
    回傳：target_date_us (mm/dd/YYYY), sb_df
    """
    now_us = datetime.now(us_east_tz)
    target_date_us = now_us.strftime("%m/%d/%Y")
    sb = fetch_safe_df(scoreboardv2.ScoreboardV2, game_date=target_date_us)

    # 只看有效隊伍的場次
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

    # 欄位保險
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
# 5) 傷病報告（ESPN）— cache（結構變動時至少不會炸）
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

        # ESPN 常改 class；先抓可能的表格容器
        tables = soup.select(".ResponsiveTable") or soup.select("section")  # fallback
        for table in tables:
            title_el = table.select_one(".Table__Title") or table.find(["h2", "h3"])
            if not title_el:
                continue
            t_name = title_el.get_text(strip=True)

            # 用英文隊名比對
            t_name_norm = t_name.lower()
            t_abbr = None
            for abbr, info in TEAM_MAP.items():
                eng = info[0].lower()
                if eng in t_name_norm:
                    t_abbr = abbr
                    break
            if not t_abbr:
                # fallback：title 可能只寫城市 / 簡寫；再做較寬鬆 contains
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

                # ESPN 通常：Player / Pos / Status / Date / Injury
                raw_player = cols[0].get_text(" ", strip=True)
                # 去掉尾端 position
                raw_player = re.sub(r"\s+(PG|SG|SF|PF|C|G|F)\s*$", "", raw_player, flags=re.I).strip()

                # 欄位容錯：Status 通常在 cols[2]，Injury/Reason 常在最後
                raw_status = cols[2].get_text(" ", strip=True).lower()
                raw_reason = cols[-1].get_text(" ", strip=True) if len(cols) >= 4 else "無"

                text_blob = (raw_status + " " + raw_reason).lower()

                # 粗略缺陣判讀
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
#    - 真 B2B：本場日期的前一天是否有比賽
#    - 近五場勝率：本場日期之前最近 5 場
# =========================================================
@st.cache_data(ttl=3600)
def get_team_context(team_ids: list[int], game_date_us: str, season: str = "2025-26") -> dict:
    """
    team_ids: 今日會出賽的隊伍 team_id list
    game_date_us: mm/dd/YYYY（美東賽程日）
    """
    ctx = {}
    game_day = datetime.strptime(game_date_us, "%m/%d/%Y").date()
    prev_day = game_day - timedelta(days=1)

    for tid in team_ids:
        log = fetch_safe_df(teamgamelog.TeamGameLog, team_id=tid, season=season)
        is_b2b, recent_w = False, 0.5

        if not log.empty and "GAME_DATE" in log.columns and "WL" in log.columns:
            # 只需要近幾場就夠了（減少處理成本）
            log = log.head(15).copy()
            log["GAME_DATE"] = pd.to_datetime(log["GAME_DATE"], errors="coerce").dt.date
            log = log.dropna(subset=["GAME_DATE"])

            # 只看本場日期之前的比賽（避免把同日/未來混進去）
            prior = log[log["GAME_DATE"] < game_day].sort_values("GAME_DATE", ascending=False)

            # 真 B2B：最近一場是否在前一天
            if not prior.empty:
                last_game_date = prior.iloc[0]["GAME_DATE"]
                is_b2b = (last_game_date == prev_day)

                # 近五場勝率（本場之前最近五場）
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
    # 先抓目標日期與 scoreboard（修正：先有賽程，再抓 context）
    target_date_us, sb = get_target_scoreboard()

    # 球員資料 + 傷病資料（cache）
    ps_db = get_player_stats(season="2025-26")
    inj_db = get_injuries()

# 解析今日賽程（有效隊伍）
if sb.empty or "HOME_TEAM_ID" not in sb.columns:
    st.info("📅 目前抓不到賽程資料（Scoreboard API 回傳空）。請稍後重試。")
    st.stop()

sb_filtered = sb[sb["HOME_TEAM_ID"].isin(VALID_TEAM_IDS)].copy()

# 今日真的沒賽程的情況
if sb_filtered.empty:
    st.info(f"📅 {target_date_us}（美東）無有效 NBA 賽程。")
    st.stop()
else:
    # 如果 target_date_us 是明天的，提示（和你原本 UI 一樣的語氣）
    now_us = datetime.now(us_east_tz).strftime("%m/%d/%Y")
    if target_date_us != now_us:
        st.info(f"📅 今日美東無賽程，已為您自動跳轉至明日：{target_date_us}")
    else:
        st.success(f"📅 正在分析美東今日賽程：{target_date_us}")

# 只針對今天出賽隊伍抓 context（效能大幅提升）
today_team_ids = sorted(set(sb_filtered["HOME_TEAM_ID"].tolist() + sb_filtered["VISITOR_TEAM_ID"].tolist()))
ctx_db = get_team_context(today_team_ids, game_date_us=target_date_us, season="2025-26")

# 傷病抓取狀態提示（避免誤判）
if inj_db.empty:
    st.warning("⚠️ 傷病名單目前抓不到（ESPN 可能改版或暫時阻擋），推薦將不會排除傷兵。")


# =========================================================
# 8) 主計算：建立每場 pkg + base_diff（保留你的核心公式，但修正 key/穩定性）
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

    # 你的原公式（保留）：B2B 與近況調整
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
# 9) UI 區塊：Top 推薦（保留原 UI）
# =========================================================
st.header("🔥 今日過盤推薦 (Top 4)")
top_recommend = sorted(all_games_data, key=lambda x: abs(x["base_diff"]), reverse=True)[:4]
t_cols = st.columns(len(top_recommend))

for idx, g in enumerate(top_recommend):
    with t_cols[idx]:
        with st.container(border=True):
            rec_side = g["h_cn"] if g["base_diff"] > 0 else g["a_cn"]
            st.subheader(f"Rank {idx+1}")
            st.write(f"**{g['label']}**")
            st.metric("戰力優勢 (Edge)", f"{abs(g['base_diff']):.1f}")
            st.success(f"首選：{rec_side}")

st.divider()


# =========================================================
# 10) UI 區塊：全部場次與實時計算（保留原 UI；修正 key 與盤口文字）
# =========================================================
st.header("🎯 全部場次與實時計算")

for i in range(0, len(all_games_data), 3):
    cols = st.columns(3)
    for j, g in enumerate(all_games_data[i : i + 3]):
        with cols[j]:
            with st.container(border=True):
                st.subheader(g["label"])

                gid = g["game_id"]
                # 盤口輸入方向容易搞反 → 文案明確化，但不改你的計算方式（仍是 base_diff + u_sp）
                u_sp = st.number_input(
                    "主隊盤口（主讓分填負｜主受讓填正）",
                    min_value=-60.0,
                    max_value=60.0,
                    value=0.0,
                    step=0.5,
                    key=f"sp_{gid}",
                )
                u_oh = st.number_input("主賠", 1.01, 5.0, 1.90, key=f"oh_{gid}")
                u_oa = st.number_input("客賠", 1.01, 5.0, 1.90, key=f"oa_{gid}")

                f_edge = g["base_diff"] + u_sp
                prob = 1 / (1 + 10 ** (-abs(f_edge) / 11)) * 100  # 你原本的映射（保留）
                rec = g["h_cn"] if f_edge > 0 else g["a_cn"]
                odds = u_oh if f_edge > 0 else u_oa
                ev = (prob / 100 * odds) - 1

                st.write(f"勝率: **{prob:.1f}%** | Edge: **{abs(f_edge):.1f}**")
                st.write(f"EV: **{ev*100:+.1f}%**")
                if ev > 0.05:
                    st.success(f"🔥 推薦：{rec}")
                else:
                    st.info(f"建議：{rec}")


# =========================================================
# 11) UI 區塊：深度查詢（保留原 UI）
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

