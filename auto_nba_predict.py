"""NBA 勝負預測 — 球迷向的數據預測網站（Streamlit）。

定位:用數據模型預測 NBA 勝負，給一般球迷看「今晚誰會贏 / 近期預測準不準」，
搭配球隊與球員數據查詢。純讀 DB(v_app_board / 數據表),不在前端跑模型。

誠實聲明:預測僅供球迷參考與娛樂,非投注建議。模型猜勝負準,但運彩賠率已反映
強弱,高命中率不等於能獲利。

視覺:霓虹電競夜場風（深紫黑底 + 霓虹青/洋紅雙色發光 + LED 數字）。卡片與勝率
拔河條以 HTML 渲染並注入自訂 CSS;資料層、查詢、快取完全不變。
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import streamlit as st

from jobs.teams import TEAM_NAME_CH
from webui import queries, render

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
    return queries.recent_games(q, n)


@st.cache_data(ttl=600)
def load_track_record() -> dict:
    """Headline accuracy: stored walk-forward number + live settled record."""
    return queries.track_record(q)


@st.cache_data(ttl=600)
def load_team_recent(abbr: str, n: int = 10) -> pd.DataFrame:
    return queries.team_recent(q, abbr, n)


@st.cache_data(ttl=600)
def load_player_recent(abbr: str, season: str) -> pd.DataFrame:
    return queries.player_recent(q, abbr, season)


@st.cache_data(ttl=600)
def latest_season() -> str:
    return queries.latest_season(q)


@st.cache_data(ttl=300)
def load_game(gid: str) -> pd.DataFrame:
    """Full board row for one game — drives the matchup detail page."""
    return queries.game(q, gid)


@st.cache_data(ttl=600)
def load_injuries(team_abbr: str, ref_date) -> pd.DataFrame:
    """Injury snapshot for a team (see queries.injuries)."""
    return queries.injuries(q, team_abbr, ref_date)


# =========================================================
# Page
# =========================================================

def _emit(chunks: list[str]) -> None:
    for chunk in chunks:
        st.markdown(chunk, unsafe_allow_html=True)


def render_detail(gid: str, acc, tr=None) -> None:
    df = load_game(gid)
    if df is None or df.empty:
        _emit(render.not_found_chunks(acc))
        return
    g = df.iloc[0]
    home, away = g["home_abbr"], g["away_abbr"]
    gdate, season = g["game_date_et"], g["season"]
    _emit(render.detail_chunks(
        g, tr or {},
        load_injuries(away, gdate), load_injuries(home, gdate),
        load_player_recent(away, season), load_player_recent(home, season),
        acc))


st.markdown(render.NEON_CSS, unsafe_allow_html=True)

tr = load_track_record()
acc = tr.get("wf_acc")

# ---------- routing: matchup detail vs board ----------
gid = st.query_params.get("game")
if gid:
    render_detail(gid, acc, tr)
    st.stop()

games = load_recent_games(30)

# ---------- top bar, hero, featured, predictions, track record ----------
_emit(render.board_chunks(games, tr, acc, dt.datetime.now(TW)))
if games.empty:
    st.info(render.EMPTY_BOARD_TEXT)
    st.stop()

# ---------- data explorer (slim team browse) ----------
season = latest_season()
st.markdown(render.team_section_head(season), unsafe_allow_html=True)
team_pick = st.selectbox("選擇球隊", sorted(TEAM_NAME_CH),
                         format_func=lambda a: f"{TEAM_NAME_CH[a]}（{a}）",
                         label_visibility="collapsed")
team_df = load_team_recent(team_pick)
player_df = load_player_recent(team_pick, season)
st.markdown(render.team_tables_html(team_pick, team_df, player_df), unsafe_allow_html=True)

with st.expander("ℹ️ 這個模型怎麼運作？"):
    st.markdown(render.explainer_md(acc))

# ---------- footer ----------
st.markdown(render.DISCLAIMER_HTML, unsafe_allow_html=True)

# ---------- sidebar ----------
if st.sidebar.button("🔄 重新整理資料"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption("NBA 勝負預測 · 數據驅動 · 僅供參考娛樂")
