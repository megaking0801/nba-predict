"""NBA 勝負預測 — 球迷向的數據預測網站（Streamlit）。

定位:用數據模型預測 NBA 勝負，給一般球迷看「今晚誰會贏 / 近期預測準不準」，
搭配球隊與球員數據查詢。純讀 DB(v_app_board / 數據表),不在前端跑模型。

誠實聲明:預測僅供球迷參考與娛樂,非投注建議。模型猜勝負準,但運彩賠率已反映
強弱,高命中率不等於能獲利。
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import streamlit as st

from jobs.model import MODEL_NAME
from jobs.teams import TEAM_NAME_CH

TW = ZoneInfo("Asia/Taipei")
ET = ZoneInfo("America/New_York")

st.set_page_config(page_title="NBA 勝負預測", page_icon="🏀", layout="wide")


# =========================================================
# DB connection (env first, st.secrets fallback for Streamlit Cloud)
# =========================================================

def _secret(key: str) -> str:
    try:
        return str(st.secrets.get(key, "") or "")
    except Exception:
        return ""


def _conn_kwargs() -> dict:
    db_url = (os.environ.get("DATABASE_URL") or "").strip() or _secret("DATABASE_URL").strip()
    if db_url:
        return {"dsn": db_url}
    cfg = {k: (os.environ.get(k) or "").strip() or _secret(k).strip()
           for k in ("SUPABASE_HOST", "SUPABASE_DB", "SUPABASE_USER",
                     "SUPABASE_PASSWORD", "SUPABASE_PORT")}
    if not cfg["SUPABASE_HOST"]:
        raise RuntimeError("DB 連線資訊缺失：請設定 DATABASE_URL 或 SUPABASE_* secrets")
    return {"host": cfg["SUPABASE_HOST"], "dbname": cfg["SUPABASE_DB"] or "postgres",
            "user": cfg["SUPABASE_USER"], "password": cfg["SUPABASE_PASSWORD"],
            "port": int(cfg["SUPABASE_PORT"] or "5432"), "sslmode": "require"}


def pg_conn():
    return psycopg2.connect(connect_timeout=8, **_conn_kwargs())


def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


# =========================================================
# Cached reads
# =========================================================

@st.cache_data(ttl=300)
def load_recent_games(n: int = 30) -> pd.DataFrame:
    """Newest predicted games — upcoming first when in season, else recent finals."""
    return q(
        """
        SELECT game_id, game_date_et, tipoff_utc, status, home_abbr, away_abbr,
               home_score, away_score, pred_margin, p_home_win, win_result
        FROM public.v_app_board
        WHERE p_home_win IS NOT NULL
        ORDER BY game_date_et DESC, tipoff_utc DESC NULLS LAST, game_id
        LIMIT %s
        """,
        (n,),
    )


@st.cache_data(ttl=600)
def load_track_record() -> dict:
    """Headline accuracy: stored walk-forward number + live settled record."""
    df = q("SELECT metrics FROM public.model_registry_v2 "
           "WHERE model_name=%s AND is_active", (MODEL_NAME,))
    wf = {}
    if not df.empty:
        m = df.iloc[0]["metrics"] if isinstance(df.iloc[0]["metrics"], dict) else {}
        wf = (m.get("report_betting") or {}).get("straight_up") or {}
    rec = q("SELECT count(*) FILTER (WHERE (p_home_win>=0.5)=(win_result=1)) AS hit, "
            "count(*) AS n FROM public.predictions "
            "WHERE p_home_win IS NOT NULL AND win_result IS NOT NULL")
    return {"wf_acc": wf.get("winner_accuracy"), "wf_n": wf.get("n"),
            "rec_hit": int(rec.iloc[0]["hit"] or 0), "rec_n": int(rec.iloc[0]["n"] or 0)}


@st.cache_data(ttl=600)
def load_team_recent(abbr: str, n: int = 10) -> pd.DataFrame:
    return q(
        """
        SELECT g.game_date_et AS 日期, g.home_abbr AS 主, g.away_abbr AS 客,
               g.home_score AS 主分, g.away_score AS 客分, t.wl AS 勝負,
               t.pts AS 得分, t.reb AS 籃板, t.ast AS 助攻, t.plus_minus AS 正負值
        FROM public.team_game_stats t
        JOIN public.games_v2 g ON g.game_id = t.game_id
        WHERE t.team_abbr = %s AND g.status = 'final'
        ORDER BY g.game_date_et DESC LIMIT %s
        """,
        (abbr, n),
    )


@st.cache_data(ttl=600)
def load_player_recent(abbr: str, season: str) -> pd.DataFrame:
    return q(
        """
        SELECT p.player_name AS 球員, count(*) AS 場次,
               round(avg(p.min_played)::numeric, 1) AS 分鐘,
               round(avg(p.pts)::numeric, 1) AS 得分,
               round(avg(p.reb)::numeric, 1) AS 籃板,
               round(avg(p.ast)::numeric, 1) AS 助攻,
               round(avg(p.plus_minus)::numeric, 1) AS 正負值
        FROM public.player_game_stats p
        JOIN public.games_v2 g ON g.game_id = p.game_id
        WHERE p.team_abbr = %s AND g.season = %s AND g.status = 'final'
        GROUP BY p.player_name HAVING avg(p.min_played) >= 10
        ORDER BY avg(p.pts) DESC
        """,
        (abbr, season),
    )


@st.cache_data(ttl=600)
def latest_season() -> str:
    df = q("SELECT season FROM public.games_v2 ORDER BY season DESC LIMIT 1")
    return df.iloc[0]["season"] if not df.empty else "2025-26"


# =========================================================
# Helpers
# =========================================================

def fmt_team(abbr: str) -> str:
    return f"{TEAM_NAME_CH.get(abbr, abbr)}"


def fmt_when(ts, date_et) -> str:
    if ts is not None and not pd.isna(ts):
        return pd.Timestamp(ts).tz_convert(TW).strftime("%m/%d %H:%M")
    return str(date_et)


def game_card(g):
    pw = float(g["p_home_win"])
    home_fav = pw >= 0.5
    fav_abbr = g["home_abbr"] if home_fav else g["away_abbr"]
    conf = pw if home_fav else 1 - pw
    final = g["status"] == "final" and pd.notna(g["win_result"])

    with st.container(border=True):
        st.markdown(f"**{fmt_team(g['away_abbr'])} ＠ {fmt_team(g['home_abbr'])}**")
        st.caption(f"🗓 {fmt_when(g['tipoff_utc'], g['game_date_et'])}（台北時間）")
        st.markdown(f"🔮 看好 **{fmt_team(fav_abbr)}** 贏")
        st.progress(min(max(conf, 0.0), 1.0), text=f"勝率 {conf * 100:.0f}%")
        if final:
            home_won = g["win_result"] == 1
            win_abbr = g["home_abbr"] if home_won else g["away_abbr"]
            correct = home_fav == home_won
            st.caption(f"🏁 終場 {fmt_team(g['away_abbr'])} {g['away_score']} - "
                       f"{g['home_score']} {fmt_team(g['home_abbr'])}"
                       f"（{fmt_team(win_abbr)}勝）")
            st.markdown("✅ **預測命中**" if correct else "❌ 預測未中")


# =========================================================
# Page
# =========================================================

st.title("🏀 NBA 勝負預測")
tr = load_track_record()
acc = tr.get("wf_acc")
if acc:
    st.markdown(f"#### 用數據預測 NBA 勝負 — 近三季實測猜中率 **{acc * 100:.0f}%**")
st.caption("⚠️ 預測由數據模型產生，僅供球迷參考與娛樂，**非投注建議**。"
           "模型擅長預測勝負，但運彩賠率已反映強弱，高命中率不等於能獲利。")

games = load_recent_games(30)
if games.empty:
    st.info("目前資料庫尚無預測。賽季開始後會自動更新每日預測。")
    st.stop()

upcoming = games[games["status"] != "final"]
header = "🔮 今日／近期預測" if not upcoming.empty else "📅 近期比賽：預測 vs 結果"
st.header(header)
cols = st.columns(3)
for i, (_, g) in enumerate(games.iterrows()):
    with cols[i % 3]:
        game_card(g)

# ---------- track record ----------
st.divider()
st.header("📊 模型戰績（誠實揭露）")
c1, c2, c3 = st.columns(3)
if acc:
    c1.metric("近三季猜中率", f"{acc * 100:.1f}%", help=f"{tr.get('wf_n')} 場時間外驗證")
if tr["rec_n"]:
    c2.metric("已結算預測", f"{tr['rec_n']} 場")
    c3.metric("其中命中", f"{tr['rec_hit'] / tr['rec_n'] * 100:.1f}%")
st.caption("「猜中率」是時間外(walk-forward)驗證：每場只用該場之前的資料預測，"
           "不偷看未來,所以是誠實、可信的數字。模型越有把握(勝率越高)的比賽,命中率越高。")

# ---------- data explorer ----------
st.divider()
st.header("🔍 球隊 ＆ 球員數據")
season = latest_season()
team_pick = st.selectbox("選擇球隊", sorted(TEAM_NAME_CH),
                         format_func=lambda a: f"{TEAM_NAME_CH[a]}（{a}）")
c1, c2 = st.columns(2)
with c1:
    st.subheader(f"{TEAM_NAME_CH[team_pick]} 近 10 場")
    st.dataframe(load_team_recent(team_pick), use_container_width=True, hide_index=True)
with c2:
    st.subheader(f"{TEAM_NAME_CH[team_pick]} 球員場均（{season}）")
    st.dataframe(load_player_recent(team_pick, season),
                 use_container_width=True, hide_index=True)

with st.expander("ℹ️ 這個模型怎麼運作？"):
    st.markdown(f"""
- **資料**：每場比賽的球隊/球員數據(來自 ESPN),共三季、4000+ 場。
- **模型**：用球隊近況、Elo 實力、四要素、休息天數、傷兵等，預測雙方分差，
  再換算成勝率。每場只用「該場之前」的資料,所以驗證數字不灌水。
- **誠實話**：猜勝負準({acc * 100:.0f}% 左右),但這不等於能在運彩賺錢——
  賠率早就反映了強弱。本站定位是**數據預測與分析**,不是穩賺明牌。
""")

if st.sidebar.button("🔄 重新整理資料"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption("NBA 勝負預測 · 數據驅動 · 僅供參考娛樂")
