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
# Neon esports theme — injected CSS
# =========================================================

NEON_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Rajdhani:wght@500;600;700&display=swap');

:root {
  --ground: #0A0815;
  --surface: rgba(24,20,48,0.62);
  --text: #ECEAFF;
  --muted: #9A93C8;
  --faint: #6C6597;
  --hair: rgba(123,91,255,0.22);
  --hair-strong: rgba(123,91,255,0.42);
  --cyan: #29E7FF;
  --magenta: #FF45C8;
  --violet: #7B5BFF;
  --win: #36FFB0;
  --miss: #FF5B7A;
  --amber: #FFC24B;
  --display: "Rajdhani", "Arial Black", "Segoe UI", system-ui, "Microsoft JhengHei", "PingFang TC", sans-serif;
  --led: "Orbitron", ui-monospace, "Consolas", "SF Mono", monospace;
  --body: "Segoe UI", system-ui, "Microsoft JhengHei", "PingFang TC", sans-serif;
}

/* ---- Streamlit shell ---- */
.stApp {
  color: var(--text);
  background:
    radial-gradient(900px 500px at 6% -8%, rgba(41,231,255,0.16), transparent 60%),
    radial-gradient(900px 600px at 100% 12%, rgba(255,69,200,0.14), transparent 60%),
    radial-gradient(700px 500px at 50% 120%, rgba(123,91,255,0.18), transparent 60%),
    var(--ground);
  background-attachment: fixed;
}
.stApp::before {
  content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0; opacity: 0.5;
  background-image:
    linear-gradient(rgba(123,91,255,0.05) 1px, transparent 1px),
    linear-gradient(90deg, rgba(123,91,255,0.05) 1px, transparent 1px);
  background-size: 46px 46px;
}
header[data-testid="stHeader"] { background: transparent; }
#MainMenu, [data-testid="stToolbar"] { visibility: hidden; }
.block-container { max-width: 1180px; padding-top: 2.2rem; padding-bottom: 3rem; }
.stApp, .stApp p, .stApp span, .stApp div, .stApp label { font-family: var(--body); }

.led { font-family: var(--led); font-variant-numeric: tabular-nums; font-weight: 700; }

/* ---- top bar ---- */
.nb-topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 20px; margin-bottom: 10px; border-radius: 14px;
  background: linear-gradient(180deg, rgba(24,20,48,0.7), rgba(24,20,48,0.35));
  border: 1px solid var(--hair);
}
.nb-brand { display: flex; align-items: center; gap: 11px; font-family: var(--display);
            font-weight: 700; font-size: 20px; letter-spacing: 0.04em; }
.nb-brand .ball { font-size: 22px; filter: drop-shadow(0 0 6px rgba(41,231,255,0.6)); }
.nb-brand .tag { font-family: var(--led); font-size: 10px; letter-spacing: 0.2em; text-transform: uppercase;
                 color: var(--cyan); border: 1px solid var(--hair-strong); border-radius: 5px; padding: 3px 8px;
                 text-shadow: 0 0 8px rgba(41,231,255,0.6); }
.nb-badge { display: flex; align-items: baseline; gap: 9px; padding: 7px 15px; border-radius: 999px;
            border: 1px solid var(--hair-strong); background: rgba(41,231,255,0.06);
            box-shadow: inset 0 0 18px -4px rgba(41,231,255,0.5); }
.nb-badge b { font-family: var(--led); font-size: 20px; color: var(--cyan); text-shadow: 0 0 12px rgba(41,231,255,0.7); }
.nb-badge span { color: var(--muted); font-size: 12px; }

/* ---- hero ---- */
.nb-hero { padding: 28px 0 10px; }
.nb-kicker { font-family: var(--led); font-size: 12px; letter-spacing: 0.22em; text-transform: uppercase;
             color: var(--magenta); text-shadow: 0 0 10px rgba(255,69,200,0.5); margin-bottom: 16px; }
.nb-hero h1 { font-family: var(--display); font-weight: 700; font-size: clamp(40px, 7vw, 84px);
              line-height: 0.96; letter-spacing: 0.01em; margin: 0 0 18px;
              text-shadow: 0 0 40px rgba(123,91,255,0.5); }
.nb-hero h1 em { font-style: normal;
                 background: linear-gradient(92deg, var(--cyan), var(--magenta));
                 -webkit-background-clip: text; background-clip: text; color: transparent;
                 filter: drop-shadow(0 0 18px rgba(255,69,200,0.35)); }
.nb-lede { max-width: 620px; color: var(--muted); font-size: 17px; margin: 0; line-height: 1.6; }

/* ---- featured ---- */
.nb-featured { margin-top: 30px; position: relative; border-radius: 18px; padding: 28px 32px 30px;
               background: var(--surface); border: 1px solid var(--hair-strong);
               box-shadow: 0 0 40px -10px rgba(41,231,255,0.30), 0 0 60px -20px rgba(255,69,200,0.30); }
.nb-ftag { font-family: var(--led); font-size: 11px; letter-spacing: 0.2em; text-transform: uppercase;
           color: var(--cyan); text-shadow: 0 0 8px rgba(41,231,255,0.6); margin-bottom: 22px;
           display: flex; align-items: center; gap: 9px; }
.nb-ftag .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--cyan);
                box-shadow: 0 0 0 4px rgba(41,231,255,0.18), 0 0 12px rgba(41,231,255,0.9); }
.nb-mbig { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 18px; }
.nb-tb { text-align: center; }
.nb-tb .abbr { font-family: var(--display); font-weight: 700; font-size: clamp(34px, 5.5vw, 60px); line-height: 0.95; }
.nb-tb .name { color: var(--muted); font-size: 14px; margin-top: 8px; }
.nb-tb.fav .abbr { color: var(--cyan); text-shadow: 0 0 22px rgba(41,231,255,0.65); }
.nb-at { font-family: var(--led); color: var(--faint); font-size: 16px; }

/* tug-of-war bars */
.nb-pbar { margin-top: 26px; }
.nb-track { position: relative; height: 46px; border-radius: 12px; overflow: hidden;
            background: rgba(255,69,200,0.10); border: 1px solid var(--hair-strong);
            display: flex; align-items: center; }
.nb-fill { position: absolute; top: 0; bottom: 0; box-shadow: 0 0 24px rgba(41,231,255,0.55); }
.nb-lbl { position: relative; z-index: 2; flex: 1; display: flex; align-items: center; gap: 9px;
          padding: 0 18px; font-family: var(--display); font-weight: 700; letter-spacing: 0.04em; color: var(--muted); }
.nb-lbl.r { justify-content: flex-end; }
.nb-lbl.fav { color: #fff; text-shadow: 0 0 12px rgba(255,255,255,0.5); }
.nb-pct { font-family: var(--led); font-size: 24px; }
.nb-read { margin-top: 20px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.nb-chip { display: inline-flex; align-items: center; gap: 9px; font-family: var(--display); font-weight: 700;
           font-size: 15px; padding: 9px 18px; border-radius: 10px; color: var(--ground);
           background: linear-gradient(92deg, var(--cyan), var(--violet));
           box-shadow: 0 0 22px -4px rgba(41,231,255,0.7); }
.nb-read .meta { color: var(--muted); font-family: var(--led); font-size: 13px; }
.nb-read .meta b { color: var(--cyan); }

/* ---- section heads ---- */
.nb-sechead { display: flex; align-items: baseline; justify-content: space-between; margin: 6px 0 20px; }
.nb-sechead h2 { font-family: var(--display); font-weight: 700; font-size: clamp(24px, 3.4vw, 34px);
                 margin: 0; text-shadow: 0 0 26px rgba(123,91,255,0.45); }
.nb-sechead h2 .bar { color: var(--magenta); text-shadow: 0 0 12px rgba(255,69,200,0.6); }
.nb-sechead .sub { color: var(--muted); font-family: var(--led); font-size: 13px; }

/* ---- date group ---- */
.nb-datehead { display: flex; align-items: center; gap: 14px; margin: 4px 0 16px; }
.nb-datehead .d { font-family: var(--led); font-size: 15px; letter-spacing: 0.04em; font-weight: 700; }
.nb-datehead .d b { color: var(--cyan); text-shadow: 0 0 10px rgba(41,231,255,0.6); }
.nb-datehead .ln { flex: 1; height: 1px; background: linear-gradient(90deg, var(--hair-strong), transparent); }
.nb-datehead .cnt { font-family: var(--led); font-size: 12px; color: var(--muted); }
.nb-group { margin-bottom: 30px; }

/* home/away badge */
.nb-ha { font-family: var(--led); font-size: 10px; font-weight: 700; letter-spacing: 0.06em;
         padding: 1px 6px; border-radius: 4px; margin-left: 5px; }
.nb-ha.home { color: var(--cyan); border: 1px solid var(--hair-strong); background: rgba(41,231,255,0.10); }
.nb-ha.away { color: var(--muted); border: 1px solid var(--hair); }

/* ---- cards ---- */
.nb-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.nb-card { background: var(--surface); border: 1px solid var(--hair); border-radius: 14px; padding: 18px;
           transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease; }
.nb-card:hover { transform: translateY(-3px); border-color: var(--hair-strong);
                 box-shadow: 0 0 30px -8px rgba(41,231,255,0.45); }
.nb-crow { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
.nb-when { font-family: var(--led); font-size: 12px; color: var(--muted); }
.nb-pill { font-family: var(--led); font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase;
           font-weight: 700; padding: 3px 9px; border-radius: 6px; }
.nb-pill.up { color: var(--cyan); border: 1px solid var(--hair-strong); background: rgba(41,231,255,0.08); }
.nb-pill.fin { color: var(--faint); border: 1px solid var(--hair); }
.nb-cmatch { display: flex; align-items: center; justify-content: center; gap: 14px; margin-bottom: 16px; }
.nb-cmatch .t { text-align: center; }
.nb-cmatch .abbr { font-family: var(--display); font-weight: 700; font-size: 28px; }
.nb-cmatch .name { font-size: 11px; color: var(--muted); }
.nb-cmatch .t.fav .abbr { color: var(--cyan); text-shadow: 0 0 16px rgba(41,231,255,0.6); }
.nb-cmatch .at { font-size: 13px; color: var(--faint); }
.nb-cbar { position: relative; height: 30px; border-radius: 8px; overflow: hidden; margin-bottom: 13px;
           background: rgba(255,69,200,0.08); border: 1px solid var(--hair); display: flex; align-items: center; }
.nb-cbar .nb-fill { box-shadow: 0 0 16px rgba(41,231,255,0.5); }
.nb-cbar .nb-lbl { padding: 0 11px; font-size: 12px; }
.nb-cbar .nb-pct { font-size: 16px; }
.nb-cverdict { font-size: 13px; color: var(--text); }
.nb-cverdict b { color: var(--cyan); }
.nb-cresult { margin-top: 12px; padding-top: 11px; border-top: 1px solid var(--hair);
              display: flex; justify-content: space-between; align-items: center; font-size: 12px; }
.nb-cresult .score { font-family: var(--led); color: var(--muted); }
.nb-hit { color: var(--win); font-weight: 700; text-shadow: 0 0 10px rgba(54,255,176,0.5); }
.nb-miss { color: var(--miss); font-weight: 700; text-shadow: 0 0 10px rgba(255,91,122,0.5); }

/* ---- track record ---- */
.nb-record { display: grid; grid-template-columns: repeat(3,1fr); gap: 16px; }
.nb-rcell { background: var(--surface); border: 1px solid var(--hair); border-radius: 14px; padding: 26px 24px; }
.nb-rcell .k { font-family: var(--led); font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); }
.nb-rcell .v { font-family: var(--led); font-size: 48px; font-weight: 700; line-height: 1; margin: 12px 0 8px; }
.nb-rcell .v.c { color: var(--cyan); text-shadow: 0 0 22px rgba(41,231,255,0.6); }
.nb-rcell .v.m { color: var(--magenta); text-shadow: 0 0 22px rgba(255,69,200,0.55); }
.nb-rcell .note { color: var(--muted); font-size: 12px; }
.nb-honest { margin-top: 16px; background: var(--surface); border: 1px solid var(--hair);
             border-left: 3px solid var(--magenta); border-radius: 12px; padding: 18px 22px;
             color: var(--muted); font-size: 13px; line-height: 1.7; }
.nb-honest b { color: var(--text); }

/* ---- data tables ---- */
.nb-tables { display: grid; grid-template-columns: 1.15fr 1fr; gap: 16px; }
.nb-panel { background: var(--surface); border: 1px solid var(--hair); border-radius: 14px; padding: 16px 20px; }
.nb-panel h3 { margin: 0 0 12px; font-family: var(--display); font-size: 16px; font-weight: 700; }
.nb-panel h3 span { color: var(--muted); font-weight: 500; font-family: var(--led); font-size: 12px; }
.nb-panel .empty { color: var(--faint); font-size: 13px; padding: 8px 0; }
table.ntable { width: 100%; border-collapse: collapse; font-size: 13px; color: var(--text); }
table.ntable thead th { text-align: right; color: var(--muted); font-family: var(--led); font-weight: 600;
                        font-size: 11px; padding: 0 0 10px; }
table.ntable thead th:first-child, table.ntable tbody td:first-child { text-align: left; }
table.ntable tbody td { padding: 8px 0; border-top: 1px solid var(--hair); text-align: right;
                        font-family: var(--led); font-variant-numeric: tabular-nums; }
table.ntable tbody td:first-child { font-family: var(--body); }

/* ---- footer + widgets ---- */
.nb-disc { background: var(--surface); border: 1px solid var(--hair); border-left: 3px solid var(--cyan);
           border-radius: 12px; padding: 18px 22px; color: var(--muted); font-size: 13px; line-height: 1.7; }
.nb-disc b { color: var(--cyan); }
.nb-sign { text-align: center; margin-top: 20px; color: var(--faint); font-family: var(--led); font-size: 12px; letter-spacing: 0.1em; }

[data-testid="stSidebar"] { background: rgba(14,11,30,0.92); border-right: 1px solid var(--hair); }
.stButton > button { background: linear-gradient(92deg, var(--cyan), var(--violet)); color: var(--ground);
                     border: none; font-weight: 700; border-radius: 9px; }
[data-baseweb="select"] > div { background: var(--surface); border-color: var(--hair-strong); }

@keyframes nbgrow { from { width: 0; } }
@keyframes nbpulse { 0%,100% { box-shadow: 0 0 0 4px rgba(41,231,255,0.18), 0 0 12px rgba(41,231,255,0.9); }
                     50% { box-shadow: 0 0 0 6px rgba(41,231,255,0.10), 0 0 18px rgba(41,231,255,1); } }
@media (prefers-reduced-motion: no-preference) {
  .nb-ftag .dot { animation: nbpulse 1.8s ease-in-out infinite; }
}
/* ---- clickable card ---- */
a.nb-cardlink { text-decoration: none; color: inherit; display: block; cursor: pointer; }
a.nb-cardlink:hover .nb-card { transform: translateY(-3px); border-color: var(--cyan);
                               box-shadow: 0 0 30px -8px rgba(41,231,255,0.55); }
.nb-cta { margin-top: 14px; text-align: center; font-family: var(--led); font-size: 12px;
          letter-spacing: 0.08em; color: var(--cyan); border: 1px solid var(--hair-strong);
          border-radius: 9px; padding: 9px; background: rgba(41,231,255,0.07);
          text-shadow: 0 0 8px rgba(41,231,255,0.4); transition: background .18s ease, box-shadow .18s ease; }
a.nb-cardlink:hover .nb-cta { background: rgba(41,231,255,0.18);
                              box-shadow: 0 0 18px -4px rgba(41,231,255,0.7); }

/* ---- detail page ---- */
.nb-back { display: inline-flex; align-items: center; gap: 6px; color: var(--cyan);
           text-decoration: none; font-family: var(--led); font-size: 13px; letter-spacing: 0.04em; }
.nb-back:hover { text-shadow: 0 0 10px rgba(41,231,255,0.6); }
.nb-detailhead { position: relative; border-radius: 18px; padding: 28px 32px; margin-top: 10px;
                 background: var(--surface); border: 1px solid var(--hair-strong);
                 box-shadow: 0 0 40px -12px rgba(123,91,255,0.4); }
.nb-detailhead .when { text-align: center; font-family: var(--led); font-size: 13px;
                       color: var(--muted); margin-top: 16px; }

.nb-bets { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; align-items: start; }
.nb-bet { background: var(--surface); border: 1px solid var(--hair); border-radius: 14px; padding: 18px 20px; }
.nb-bet h4 { margin: 0 0 14px; font-family: var(--display); font-size: 17px; font-weight: 700;
             display: flex; align-items: baseline; gap: 8px; }
.nb-bet h4 span { font-family: var(--led); font-size: 11px; color: var(--muted);
                  text-transform: uppercase; letter-spacing: 0.1em; }
.nb-bet .line { font-family: var(--led); font-size: 14px; color: var(--text); margin-bottom: 12px; }
.nb-bet .line b { color: var(--cyan); }
.nb-bet .verdict { margin-top: 14px; font-size: 13px; color: var(--text); }
.nb-bet .verdict b { color: var(--cyan); }
.nb-bet .res { margin-top: 12px; padding-top: 11px; border-top: 1px solid var(--hair); font-size: 13px;
               display: flex; justify-content: space-between; align-items: center; }
.nb-bet .res .score { font-family: var(--led); color: var(--muted); }
.nb-bet.disabled { opacity: 0.5; }
.nb-bet.disabled .soon { font-family: var(--led); color: var(--muted); font-size: 13px; margin-top: 8px; line-height: 1.6; }
.nb-bet .empty { color: var(--faint); font-size: 13px; }
/* totals: estimate-first hero */
.nb-totline { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px 12px; margin: 4px 0 2px; }
.nb-totline .led.big { font-family: var(--led); font-weight: 900; font-size: 40px; line-height: 1;
                       color: var(--cyan); text-shadow: 0 0 18px rgba(41,231,255,0.35); }
.nb-totline .vs { font-size: 12px; color: var(--muted); letter-spacing: 0.04em; }
.nb-lean { font-family: var(--led); font-size: 13px; font-weight: 700; padding: 3px 10px; border-radius: 999px;
           border: 1px solid currentColor; letter-spacing: 0.02em; }
.nb-lean.over  { color: var(--magenta); }
.nb-lean.under { color: var(--cyan); }
.nb-lean.push  { color: var(--muted); }
.nb-caveat { margin-top: 12px; font-size: 11.5px; line-height: 1.6; color: var(--amber);
             background: rgba(255,194,75,0.07); border: 1px solid rgba(255,194,75,0.22);
             border-radius: 9px; padding: 8px 11px; }
.nb-caveat b { color: var(--amber); }

.nb-twocol { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.nb-inj { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-top: 1px solid var(--hair); font-size: 13px; }
.nb-inj:first-child { border-top: none; }
.nb-inj .pn { flex: 1; }
.nb-inj .det { color: var(--muted); font-size: 12px; }
.nb-injstatus { font-family: var(--led); font-size: 10px; font-weight: 700; letter-spacing: 0.06em;
                padding: 2px 8px; border-radius: 5px; text-transform: uppercase; white-space: nowrap; }
.nb-injstatus.out { color: var(--miss); border: 1px solid var(--miss); }
.nb-injstatus.dtd { color: var(--amber); border: 1px solid var(--amber); }

@media (max-width: 880px) {
  .nb-grid, .nb-tables, .nb-record, .nb-bets, .nb-twocol { grid-template-columns: 1fr; }
}
</style>
"""

DISCLAIMER_HTML = (
    '<div class="nb-disc">⚠️ 預測由數據模型產生，<b>僅供球迷參考與娛樂，非投注建議</b>。'
    '模型擅長預測勝負，但運彩賠率已反映強弱，高命中率不等於能獲利。<br>'
    '資料來源：每場球隊／球員數據（ESPN），共三季、4000+ 場。模型用近況、Elo 實力、'
    '四要素、休息天數、傷兵等預測雙方分差，再換算成勝率。</div>'
    '<div class="nb-sign">NBA 勝負預測 · 數據驅動 · 僅供參考娛樂</div>'
)


# =========================================================
# Helpers
# =========================================================

_WD = ["一", "二", "三", "四", "五", "六", "日"]


def fmt_team(abbr: str) -> str:
    return f"{TEAM_NAME_CH.get(abbr, abbr)}"


# ESPN injury text → Chinese. status is exact; detail matches whole phrase first,
# then word-by-word (body part + injury type) so combos like "Knee Surgery" work.
INJ_STATUS_CH = {
    "out": "缺陣", "day-to-day": "每日觀察", "doubtful": "出賽成疑",
    "questionable": "出賽待定", "probable": "大致可上", "available": "可出賽",
    "game time decision": "臨場決定", "suspension": "禁賽", "suspended": "禁賽",
}
INJ_TERM_CH = {
    # injury types
    "surgery": "手術", "sprain": "扭傷", "strain": "拉傷", "not specified": "未說明",
    "soreness": "痠痛", "bruise": "挫傷", "contusion": "挫傷", "fracture": "骨折",
    "pinched nerve": "神經夾擠", "tendinitis": "肌腱炎", "inflammation": "發炎",
    "illness": "生病", "rest": "輪休", "concussion": "腦震盪", "tear": "撕裂",
    "rupture": "斷裂", "spasm": "痙攣", "dislocation": "脫臼", "laceration": "撕裂傷",
    "stinger": "神經拉傷", "tightness": "緊繃", "personal": "個人因素",
    # body parts
    "knee": "膝蓋", "ankle": "腳踝", "hamstring": "腿後肌", "back": "背部",
    "shoulder": "肩膀", "wrist": "手腕", "foot": "足部", "hip": "髖部", "calf": "小腿",
    "groin": "鼠蹊", "achilles": "阿基里斯腱", "hand": "手部", "finger": "手指",
    "toe": "腳趾", "elbow": "手肘", "neck": "頸部", "quad": "股四頭肌",
    "quadriceps": "股四頭肌", "thigh": "大腿", "thumb": "拇指", "rib": "肋骨",
    "abdominal": "腹部", "oblique": "腹斜肌", "heel": "腳跟", "leg": "腿部",
    "arm": "手臂", "chest": "胸部", "nose": "鼻部", "eye": "眼部", "face": "臉部",
}


def tw_status(s) -> str:
    return INJ_STATUS_CH.get(str(s or "").strip().lower(), str(s or ""))


def tw_detail(d) -> str:
    d = str(d or "").strip()
    if not d:
        return ""
    if d.lower() in INJ_TERM_CH:
        return INJ_TERM_CH[d.lower()]
    return " ".join(INJ_TERM_CH.get(t.lower(), t) for t in d.split())


def fmt_when(ts, date_et) -> str:
    if ts is not None and not pd.isna(ts):
        return pd.Timestamp(ts).tz_convert(TW).strftime("%m/%d %H:%M")
    return str(date_et)


def _conf_word(p_home: float) -> str:
    edge = abs(float(p_home) - 0.5)
    if edge >= 0.15:
        return "高"
    if edge >= 0.07:
        return "中"
    return "低"


def _fill_html(home_fav: bool, fav_pct: int, big: bool = False) -> str:
    """Glowing tug-of-war fill anchored on the favored team's side."""
    if home_fav:  # favored team is on the right
        grad = "linear-gradient(270deg, rgba(123,91,255,0.45), var(--cyan))"
        anchor = "right:0;left:auto;"
    else:         # favored team is on the left
        grad = "linear-gradient(90deg, var(--cyan), rgba(123,91,255,0.45))"
        anchor = "left:0;right:auto;"
    return (f'<div class="nb-fill" style="{anchor}width:{fav_pct}%;'
            f'background:{grad};"></div>')


def matchup_block(g, big: bool = False) -> str:
    """Away ＠ Home with 主/客 badges; favored side highlighted in cyan."""
    p_home = float(g["p_home_win"])
    home_fav = p_home >= 0.5
    away, home = g["away_abbr"], g["home_abbr"]
    tb, at = ("nb-tb", "nb-at") if big else ("t", "at")
    away_cls = " fav" if not home_fav else ""
    home_cls = " fav" if home_fav else ""
    away_name = f'{fmt_team(away)}<span class="nb-ha away">客</span>'
    home_name = f'{fmt_team(home)}<span class="nb-ha home">主</span>'
    return (
        f'<div class="{"nb-mbig" if big else "nb-cmatch"}">'
        f'<div class="{tb}{away_cls}"><div class="abbr">{away}</div>'
        f'<div class="name">{away_name}</div></div>'
        f'<span class="{at}">＠</span>'
        f'<div class="{tb}{home_cls}"><div class="abbr">{home}</div>'
        f'<div class="name">{home_name}</div></div>'
        f'</div>'
    )


def prob_bar(g, big: bool = False) -> str:
    p_home = float(g["p_home_win"])
    home_fav = p_home >= 0.5
    home_pct = round(p_home * 100)
    away_pct = 100 - home_pct
    fav_pct = home_pct if home_fav else away_pct
    away, home = g["away_abbr"], g["home_abbr"]
    track = "nb-track" if big else "nb-track nb-cbar"
    l_fav = "" if home_fav else " fav"
    r_fav = " fav" if home_fav else ""
    return (
        f'<div class="{track}">'
        f'{_fill_html(home_fav, fav_pct, big)}'
        f'<div class="nb-lbl l{l_fav}">{away} <span class="nb-pct led">{away_pct}%</span></div>'
        f'<div class="nb-lbl r{r_fav}"><span class="nb-pct led">{home_pct}%</span> {home}</div>'
        f'</div>'
    )


def game_card_html(g) -> str:
    p_home = float(g["p_home_win"])
    home_fav = p_home >= 0.5
    fav_abbr = g["home_abbr"] if home_fav else g["away_abbr"]
    side = "主" if home_fav else "客"
    final = g["status"] == "final" and pd.notna(g["win_result"])
    pill = ('<span class="nb-pill fin">終場</span>' if final
            else '<span class="nb-pill up">尚未開賽</span>')
    result = ""
    if final:
        home_won = g["win_result"] == 1
        win_abbr = g["home_abbr"] if home_won else g["away_abbr"]
        correct = home_fav == home_won
        badge = ('<span class="nb-hit">✅ 命中</span>' if correct
                 else '<span class="nb-miss">❌ 未中</span>')
        result = (
            f'<div class="nb-cresult"><span class="score">'
            f'{g["away_abbr"]} {g["away_score"]} – {g["home_score"]} {g["home_abbr"]}'
            f'（{fmt_team(win_abbr)}勝）</span>{badge}</div>'
        )
    return (
        f'<a class="nb-cardlink" href="?game={g["game_id"]}" target="_self">'
        f'<div class="nb-card">'
        f'<div class="nb-crow"><span class="nb-when">🗓 {fmt_when(g["tipoff_utc"], g["game_date_et"])}</span>{pill}</div>'
        f'{matchup_block(g, big=False)}'
        f'{prob_bar(g, big=False)}'
        f'<div class="nb-cverdict">🔮 看好 <b>{fmt_team(fav_abbr)}</b>（{side}）贏</div>'
        f'{result}'
        f'<div class="nb-cta">🔍 點看詳細玩法 · 傷病 · 球員 →</div>'
        f'</div>'
        f'</a>'
    )


def featured_html(g) -> str:
    p_home = float(g["p_home_win"])
    home_fav = p_home >= 0.5
    fav_abbr = g["home_abbr"] if home_fav else g["away_abbr"]
    side = "主" if home_fav else "客"
    return (
        f'<div class="nb-featured">'
        f'<div class="nb-ftag"><span class="dot"></span>今晚最有把握的一場</div>'
        f'{matchup_block(g, big=True)}'
        f'<div class="nb-pbar">{prob_bar(g, big=True)}</div>'
        f'<div class="nb-read"><span class="nb-chip">🔮 看好 {fmt_team(fav_abbr)}（{side}）贏</span>'
        f'<span class="meta">信心 <b>{_conf_word(p_home)}</b>　·　🗓 {fmt_when(g["tipoff_utc"], g["game_date_et"])}（台北時間）</span>'
        f'</div></div>'
    )


def date_group_html(date_val, rows: list) -> str:
    ts = pd.Timestamp(date_val)
    label = ts.strftime("%m/%d")
    wd = f"週{_WD[ts.weekday()]}"
    n = len(rows)
    finals = sum(1 for r in rows if r["status"] == "final")
    if finals == n:
        word = "已結束"
    elif finals == 0:
        word = "尚未開賽"
    else:
        word = f"{n - finals} 場待打"
    cards = "".join(game_card_html(r) for r in rows)
    return (
        f'<div class="nb-group">'
        f'<div class="nb-datehead"><span class="d"><b>{label}</b> {wd}</span>'
        f'<span class="cnt">{n} 場 · {word}</span><span class="ln"></span></div>'
        f'<div class="nb-grid">{cards}</div></div>'
    )


def table_html(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return '<div class="empty">尚無資料</div>'
    return df.to_html(index=False, classes="ntable", border=0, escape=False)


def topbar_html(acc, back: bool = False) -> str:
    badge = (f'<div class="nb-badge"><b class="led">{acc * 100:.0f}%</b>'
             f'<span>近三季猜中率</span></div>') if acc else ""
    bar = (f'<div class="nb-topbar"><div class="nb-brand"><span class="ball">🏀</span>'
           f'NBA 勝負預測 <span class="tag">live model</span></div>{badge}</div>')
    if back:
        return ('<a class="nb-back" href="?" target="_self">← 返回看板</a>'
                f'<div style="height:10px;"></div>{bar}')
    return bar


# ----- matchup detail page -----

def _bet_moneyline(g) -> str:
    p_home = float(g["p_home_win"])
    home_fav = p_home >= 0.5
    fav_abbr = g["home_abbr"] if home_fav else g["away_abbr"]
    side = "主" if home_fav else "客"
    res = ""
    if g["status"] == "final" and pd.notna(g["win_result"]):
        home_won = g["win_result"] == 1
        correct = home_fav == home_won
        win_abbr = g["home_abbr"] if home_won else g["away_abbr"]
        badge = ('<span class="nb-hit">✅ 命中</span>' if correct
                 else '<span class="nb-miss">❌ 未中</span>')
        res = (f'<div class="res"><span class="score">{fmt_team(win_abbr)} 勝</span>{badge}</div>')
    return (
        '<div class="nb-bet"><h4>勝負 <span>Moneyline</span></h4>'
        f'{prob_bar(g, big=True)}'
        f'<div class="verdict">🔮 看好 <b>{fmt_team(fav_abbr)}</b>（{side}）勝</div>'
        f'{res}</div>'
    )


def _bet_spread(g) -> str:
    hs, pc = g["home_spread"], g["p_home_cover"]
    if hs is None or pd.isna(hs) or pc is None or pd.isna(pc):
        return ('<div class="nb-bet"><h4>受讓分 <span>Spread</span></h4>'
                '<div class="empty">這場沒有盤口資料。</div></div>')
    hs, pc = float(hs), float(pc)
    home, away = g["home_abbr"], g["away_abbr"]
    home_cov = pc >= 0.5
    home_pct = round(pc * 100)
    away_pct = 100 - home_pct
    fav_pct = home_pct if home_cov else away_pct
    l_fav = "" if home_cov else " fav"
    r_fav = " fav" if home_cov else ""
    bar = (f'<div class="nb-track">{_fill_html(home_cov, fav_pct, True)}'
           f'<div class="nb-lbl l{l_fav}">{away} <span class="nb-pct led">{away_pct}%</span></div>'
           f'<div class="nb-lbl r{r_fav}"><span class="nb-pct led">{home_pct}%</span> {home}</div></div>')
    cov_abbr = home if home_cov else away
    cov_side = "主" if home_cov else "客"
    line = f'客 {away} {(-hs):+.1f}　／　主 {home} {hs:+.1f}'
    src = (g["line_source"] or "").upper() or "—"
    res = ""
    if g["status"] == "final" and pd.notna(g["cover_result"]):
        cr = int(g["cover_result"])
        if cr == 2:
            res = '<div class="res"><span class="score">和盤 push</span><span>退回</span></div>'
        else:
            home_covered = cr == 1
            cov_actual = home if home_covered else away
            correct = home_covered == home_cov
            badge = ('<span class="nb-hit">✅ 命中</span>' if correct
                     else '<span class="nb-miss">❌ 未中</span>')
            res = f'<div class="res"><span class="score">{fmt_team(cov_actual)} 破盤</span>{badge}</div>'
    return (
        '<div class="nb-bet"><h4>受讓分 <span>Spread</span></h4>'
        f'<div class="line">{line}　·　<b>{src}</b> 盤口</div>'
        f'{bar}'
        f'<div class="verdict">🔮 看好 <b>{fmt_team(cov_abbr)}</b>（{cov_side}）破受讓盤</div>'
        f'{res}</div>'
    )


def _bet_total(g, over_acc=None) -> str:
    """Estimate-first totals card: lead with the model's projected total vs the
    market line; the over/under *direction* is shown only as a soft lean with an
    honest caveat (walk-forward O/U accuracy is barely above a coin flip)."""
    tl = g.get("total_line")
    pt = g.get("pred_total")
    has_line = tl is not None and not pd.isna(tl)
    has_pred = pt is not None and not pd.isna(pt)
    src = (g.get("line_source") or "").upper() or "—"
    head = '<div class="nb-bet"><h4>大小分 <span>O/U</span></h4>'
    caveat = (
        f'<div class="nb-caveat">⚠ 本卡以「模型估總分」為主。破大／破小方向時間外命中率僅約 '
        f'<b>{over_acc * 100:.0f}%</b>，盤口效率高、難穩定贏盤，方向僅供參考。</div>'
        if over_acc else
        '<div class="nb-caveat">⚠ 本卡以「模型估總分」為主，破大／破小方向僅供參考。</div>'
    )

    if not has_pred:
        if has_line:
            return (f'{head}'
                    f'<div class="line">盤口總分 <b>{float(tl):.1f}</b>　·　<b>{src}</b></div>'
                    '<div class="empty">本季開賽後顯示模型估總分與大小分傾向。</div>'
                    f'{caveat}</div>')
        return ('<div class="nb-bet disabled"><h4>大小分 <span>O/U</span></h4>'
                '<div class="soon">🔧 本季開賽後顯示——模型估總分、盤口線與大小分傾向。</div></div>')

    pt = float(pt)
    pick = None  # None = no comparable line / 貼盤; True = lean over; False = lean under
    if has_line:
        diff = pt - float(tl)
        if abs(diff) < 0.5:
            lean = '<span class="nb-lean push">≈ 貼盤</span>'
        elif diff > 0:
            lean = f'<span class="nb-lean over">偏大 +{diff:.1f}</span>'
            pick = True
        else:
            lean = f'<span class="nb-lean under">偏小 {diff:.1f}</span>'
            pick = False
        lineblk = f'<div class="line">盤口總分 <b>{float(tl):.1f}</b>　·　<b>{src}</b></div>'
        hero = (f'<div class="nb-totline"><span class="led big">{pt:.0f}</span>'
                f'<span class="vs">模型估總分</span>{lean}</div>')
    else:
        lineblk = f'<div class="line">無總分盤口可比較　·　<b>{src}</b></div>'
        hero = (f'<div class="nb-totline"><span class="led big">{pt:.0f}</span>'
                f'<span class="vs">模型估總分</span></div>')

    res = ""
    if g["status"] == "final" and pd.notna(g.get("over_result")):
        orr = int(g["over_result"])
        if orr == 2:
            res = '<div class="res"><span class="score">和盤 push</span><span>退回</span></div>'
        elif pick is not None:
            went_over = orr == 1
            correct = went_over == pick
            actual = "大" if went_over else "小"
            badge = ('<span class="nb-hit">✅ 命中</span>' if correct
                     else '<span class="nb-miss">❌ 未中</span>')
            res = f'<div class="res"><span class="score">開出 {actual}分</span>{badge}</div>'
    return f'{head}{lineblk}{hero}{caveat}{res}</div>'


def injury_panel(team_abbr: str, ref_date, who: str) -> str:
    df = load_injuries(team_abbr, ref_date)
    if df is None or df.empty:
        return (f'<div class="nb-panel"><h3>{fmt_team(team_abbr)} '
                f'<span>· {who} · 傷病</span></h3><div class="empty">（無傷病回報）</div></div>')
    snap = df.iloc[0]["snap_date"]
    snap_lbl = (pd.Timestamp(snap).strftime("%m/%d 回報")
                if snap is not None and not pd.isna(snap) else "")
    rows = []
    for _, r in df.iterrows():
        raw = str(r["status"] or "")
        cls = "out" if "out" in raw.lower() else "dtd"
        stt = tw_status(raw)
        det = tw_detail(r["detail"])
        rows.append(
            f'<div class="nb-inj"><span class="pn">{r["player_name"]}</span>'
            f'<span class="nb-injstatus {cls}">{stt}</span>'
            f'<span class="det">{det}</span></div>'
        )
    return (f'<div class="nb-panel"><h3>{fmt_team(team_abbr)} '
            f'<span>· {who} · 傷病 {snap_lbl}</span></h3>{"".join(rows)}</div>')


def render_detail(gid: str, acc, tr=None) -> None:
    df = load_game(gid)
    if df is None or df.empty:
        st.markdown(topbar_html(acc, back=True), unsafe_allow_html=True)
        st.markdown('<div class="nb-honest">查無此場比賽。</div>', unsafe_allow_html=True)
        return
    g = df.iloc[0]
    home, away = g["home_abbr"], g["away_abbr"]
    season = g["season"]
    gdate = g["game_date_et"]
    final = g["status"] == "final"

    st.markdown(topbar_html(acc, back=True), unsafe_allow_html=True)

    score = ""
    if final and pd.notna(g["home_score"]):
        score = f'　·　{away} {int(g["away_score"])} – {int(g["home_score"])} {home}'
    st.markdown(
        f'<div class="nb-detailhead">{matchup_block(g, big=True)}'
        f'<div class="when">🗓 {fmt_when(g["tipoff_utc"], gdate)}（台北時間）· '
        f'{"終場" if final else "尚未開賽"}{score}</div></div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div class="nb-sechead" style="margin-top:30px;"><h2>玩法 <span class="bar">·</span> 預測</h2>'
        '<span class="sub">勝負 · 受讓分 · 大小分</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="nb-bets">{_bet_moneyline(g)}{_bet_spread(g)}'
        f'{_bet_total(g, (tr or {}).get("over_wf_acc"))}</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div class="nb-sechead" style="margin-top:36px;"><h2>傷病 <span class="bar">·</span> 名單</h2>'
        '<span class="sub">最新回報（≤ 該場日期）</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="nb-twocol">{injury_panel(away, gdate, "客")}'
        f'{injury_panel(home, gdate, "主")}</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        f'<div class="nb-sechead" style="margin-top:36px;"><h2>球員 <span class="bar">·</span> 場均</h2>'
        f'<span class="sub">season {season} · min ≥ 10</span></div>',
        unsafe_allow_html=True,
    )
    ap, hp = load_player_recent(away, season), load_player_recent(home, season)
    st.markdown(
        f'<div class="nb-twocol">'
        f'<div class="nb-panel"><h3>{fmt_team(away)} <span>· 客</span></h3>{table_html(ap)}</div>'
        f'<div class="nb-panel"><h3>{fmt_team(home)} <span>· 主</span></h3>{table_html(hp)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown(DISCLAIMER_HTML, unsafe_allow_html=True)


# =========================================================
# Page
# =========================================================

st.markdown(NEON_CSS, unsafe_allow_html=True)

tr = load_track_record()
acc = tr.get("wf_acc")

# ---------- routing: matchup detail vs board ----------
gid = st.query_params.get("game")
if gid:
    render_detail(gid, acc, tr)
    st.stop()

games = load_recent_games(30)

# ---------- top bar ----------
st.markdown(topbar_html(acc), unsafe_allow_html=True)

# ---------- hero + featured ----------
upcoming = games[games["status"] != "final"] if not games.empty else games
n_up = len(upcoming)
if n_up:
    kicker = f"{dt.datetime.now(TW):%Y.%m.%d}　今晚 {n_up} 場 · 台北時間"
    headline = "今晚<br><em>誰會贏？</em>"
    sub = ("三季、4000+ 場比賽訓練的模型，逐場輸出勝率。每場只用「該場之前」的資料，"
           "不偷看未來——所以這些數字誠實、可信。")
else:
    kicker = f"{dt.datetime.now(TW):%Y.%m.%d}　休賽季 · 近期回顧"
    headline = "近期<br><em>預測 vs 結果</em>"
    sub = ("目前 NBA 休賽季、暫無賽事。以下回顧近期比賽的預測與實際結果，"
           "以及模型的長期戰績。賽季開始後會自動恢復每日預測。")
st.markdown(
    f'<div class="nb-hero"><div class="nb-kicker">{kicker}</div>'
    f'<h1>{headline}</h1><p class="nb-lede">{sub}</p></div>',
    unsafe_allow_html=True,
)

if games.empty:
    st.info("目前資料庫尚無預測。賽季開始後會自動更新每日預測。")
    st.stop()

# featured "game of the night" — only meaningful when games are actually upcoming
if not upcoming.empty:
    feat = upcoming.loc[(upcoming["p_home_win"].astype(float) - 0.5).abs().idxmax()]
    st.markdown(featured_html(feat), unsafe_allow_html=True)

# ---------- predictions grouped by date ----------
preds_title = ("今日 <span class=\"bar\">·</span> 近期預測" if n_up
               else "近期 <span class=\"bar\">·</span> 預測 vs 結果")
st.markdown(
    f'<div class="nb-sechead" style="margin-top:40px;"><h2>{preds_title}</h2>'
    f'<span class="sub">依日期分組 · 客 ＠ 主</span></div>',
    unsafe_allow_html=True,
)
for date_val, grp in games.groupby("game_date_et", sort=False):
    rows = [r for _, r in grp.iterrows()]
    st.markdown(date_group_html(date_val, rows), unsafe_allow_html=True)

# ---------- track record ----------
st.markdown(
    '<div class="nb-sechead" style="margin-top:24px;"><h2>模型戰績 <span class="bar">·</span> 誠實揭露</h2>'
    '<span class="sub">walk-forward · 不灌水</span></div>',
    unsafe_allow_html=True,
)
acc_cell = (f'<div class="nb-rcell"><div class="k">近三季猜中率</div>'
            f'<div class="v c led">{acc * 100:.1f}%</div>'
            f'<div class="note">{tr.get("wf_n")} 場時間外驗證</div></div>') if acc else \
           ('<div class="nb-rcell"><div class="k">近三季猜中率</div>'
            '<div class="v led">—</div><div class="note">尚未訓練</div></div>')
if tr["rec_n"]:
    hit_pct = tr["rec_hit"] / tr["rec_n"] * 100
    rec_cells = (
        f'<div class="nb-rcell"><div class="k">已結算預測</div><div class="v led">{tr["rec_n"]}</div>'
        f'<div class="note">上線以來實際場次</div></div>'
        f'<div class="nb-rcell"><div class="k">其中命中</div><div class="v m led">{hit_pct:.1f}%</div>'
        f'<div class="note">{tr["rec_hit"]} / {tr["rec_n"]} 場</div></div>'
    )
else:
    rec_cells = (
        '<div class="nb-rcell"><div class="k">已結算預測</div><div class="v led">0</div>'
        '<div class="note">尚無已結算場次</div></div>'
        '<div class="nb-rcell"><div class="k">其中命中</div><div class="v led">—</div>'
        '<div class="note">等待結算</div></div>'
    )
over_acc = tr.get("over_wf_acc")
over_cells = ""
if over_acc:
    over_cells += (f'<div class="nb-rcell"><div class="k">大小分時間外猜中率</div>'
                   f'<div class="v m led">{over_acc * 100:.1f}%</div>'
                   f'<div class="note">{tr.get("over_wf_n")} 場 O/U 驗證</div></div>')
if tr.get("over_rec_n"):
    op = tr["over_rec_hit"] / tr["over_rec_n"] * 100
    over_cells += (f'<div class="nb-rcell"><div class="k">大小分已結算命中</div>'
                   f'<div class="v led">{op:.1f}%</div>'
                   f'<div class="note">{tr["over_rec_hit"]} / {tr["over_rec_n"]} 場</div></div>')
st.markdown(f'<div class="nb-record">{acc_cell}{rec_cells}{over_cells}</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="nb-honest">「猜中率」是每場只用該場之前的資料、不偷看未來算出來的，所以誠實可信；'
    '模型越有把握，命中率越高。<b>但這不等於能賺錢</b>——運彩賠率早已反映強弱，高命中率 ≠ 能獲利。'
    '本站是數據預測與分析，不是穩賺明牌。</div>',
    unsafe_allow_html=True,
)

# ---------- data explorer (slim team browse) ----------
season = latest_season()
st.markdown(
    f'<div class="nb-sechead" style="margin-top:36px;"><h2>🔍 任選球隊 <span class="bar">·</span> 看數據</h2>'
    f'<span class="sub">season {season} · 點上方對戰可看雙隊對比</span></div>',
    unsafe_allow_html=True,
)
team_pick = st.selectbox("選擇球隊", sorted(TEAM_NAME_CH),
                         format_func=lambda a: f"{TEAM_NAME_CH[a]}（{a}）",
                         label_visibility="collapsed")
team_df = load_team_recent(team_pick)
player_df = load_player_recent(team_pick, season)
st.markdown(
    f'<div class="nb-tables">'
    f'<div class="nb-panel"><h3>{TEAM_NAME_CH[team_pick]} 近 10 場 <span>主＝主場</span></h3>'
    f'{table_html(team_df)}</div>'
    f'<div class="nb-panel"><h3>{TEAM_NAME_CH[team_pick]} 球員場均 <span>min ≥ 10</span></h3>'
    f'{table_html(player_df)}</div>'
    f'</div>',
    unsafe_allow_html=True,
)

with st.expander("ℹ️ 這個模型怎麼運作？"):
    st.markdown(f"""
- **資料**：每場比賽的球隊/球員數據(來自 ESPN),共三季、4000+ 場。
- **模型**：用球隊近況、Elo 實力、四要素、休息天數、傷兵等，預測雙方分差，
  再換算成勝率。每場只用「該場之前」的資料,所以驗證數字不灌水。
- **誠實話**：猜勝負準({acc * 100:.0f}% 左右),但這不等於能在運彩賺錢——
  賠率早就反映了強弱。本站定位是**數據預測與分析**,不是穩賺明牌。
""" if acc else """
- **資料**：每場比賽的球隊/球員數據(來自 ESPN),共三季、4000+ 場。
- **模型**：用球隊近況、Elo 實力、四要素、休息天數、傷兵等，預測雙方分差，
  再換算成勝率。每場只用「該場之前」的資料,所以驗證數字不灌水。
- **誠實話**：模型猜勝負準,但這不等於能在運彩賺錢——賠率早就反映了強弱。
""")

# ---------- footer ----------
st.markdown(DISCLAIMER_HTML, unsafe_allow_html=True)

# ---------- sidebar ----------
if st.sidebar.button("🔄 重新整理資料"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption("NBA 勝負預測 · 數據驅動 · 僅供參考娛樂")
