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

from jobs.model import MODEL_NAME
from jobs.teams import TEAM_NAME_CH
from webui import render

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

    # totals (大小分): walk-forward O/U accuracy + live settled O/U record
    tdf = q("SELECT metrics FROM public.model_registry_v2 "
            "WHERE model_name=%s AND is_active", ("total_model",))
    over = {}
    if not tdf.empty:
        tm = tdf.iloc[0]["metrics"] if isinstance(tdf.iloc[0]["metrics"], dict) else {}
        over = (tm.get("report_over") or {}) if isinstance(tm, dict) else {}
    reco = q("SELECT count(*) FILTER (WHERE (p_over>=0.5)=(over_result=1)) AS hit, "
             "count(*) AS n FROM public.predictions "
             "WHERE p_over IS NOT NULL AND over_result IN (0,1)")
    return {"wf_acc": wf.get("winner_accuracy"), "wf_n": wf.get("n"),
            "rec_hit": int(rec.iloc[0]["hit"] or 0), "rec_n": int(rec.iloc[0]["n"] or 0),
            "over_wf_acc": over.get("accuracy"), "over_wf_n": over.get("n"),
            "over_rec_hit": int(reco.iloc[0]["hit"] or 0),
            "over_rec_n": int(reco.iloc[0]["n"] or 0)}


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


@st.cache_data(ttl=300)
def load_game(gid: str) -> pd.DataFrame:
    """Full board row for one game — drives the matchup detail page."""
    return q("SELECT * FROM public.v_app_board WHERE game_id = %s", (gid,))


@st.cache_data(ttl=600)
def load_injuries(team_abbr: str, ref_date) -> pd.DataFrame:
    """Injury snapshot for a team: latest on/before ref_date, else most recent
    available (so past games still show current injuries). Out listed first.
    Includes snap_date so the UI can show which day the report is from."""
    return q(
        """
        WITH chosen AS (
          SELECT COALESCE(
            (SELECT max(snapshot_date_et) FROM public.injury_snapshots
             WHERE team_abbr = %s AND snapshot_date_et <= %s),
            (SELECT max(snapshot_date_et) FROM public.injury_snapshots
             WHERE team_abbr = %s)
          ) AS d
        )
        SELECT player_name, status, detail, (SELECT d FROM chosen) AS snap_date
        FROM public.injury_snapshots
        WHERE team_abbr = %s AND snapshot_date_et = (SELECT d FROM chosen)
        ORDER BY (status ILIKE 'out') DESC, player_name
        """,
        (team_abbr, ref_date, team_abbr, team_abbr),
    )


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
