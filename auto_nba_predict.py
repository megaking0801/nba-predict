"""NBA Edge v2 — Streamlit board over the rebuilt pipeline.

The app is a pure DB reader plus one writer: manual line entry into
market_lines (source='manual'). All data production (ingest, features,
predictions, settlement) happens in the jobs/ pipeline. Probability math is
shared with the pipeline via jobs.model / jobs.picks — closed-form only, the
app never unpickles sklearn models.
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import streamlit as st

from jobs.config import CONFIG
from jobs.model import CALIBRATOR_NAME, MODEL_NAME
from jobs.picks import decide_game, effective_min_edge
from jobs.teams import TEAM_NAME_CH

TW = ZoneInfo("Asia/Taipei")
ET = ZoneInfo("America/New_York")

st.set_page_config(page_title="NBA Edge v2 + Supabase + ML", layout="wide")

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
    cfg = {}
    for k in ("SUPABASE_HOST", "SUPABASE_DB", "SUPABASE_USER",
              "SUPABASE_PASSWORD", "SUPABASE_PORT"):
        cfg[k] = (os.environ.get(k) or "").strip() or _secret(k).strip()
    if not cfg["SUPABASE_HOST"]:
        raise RuntimeError("DB 連線資訊缺失：請設定 DATABASE_URL 或 SUPABASE_* secrets")
    return {
        "host": cfg["SUPABASE_HOST"],
        "dbname": cfg["SUPABASE_DB"] or "postgres",
        "user": cfg["SUPABASE_USER"],
        "password": cfg["SUPABASE_PASSWORD"],
        "port": int(cfg["SUPABASE_PORT"] or "5432"),
        "sslmode": "require",
    }


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
def load_board(date_from: dt.date, date_to: dt.date) -> pd.DataFrame:
    return q(
        "SELECT * FROM public.v_app_board WHERE game_date_et BETWEEN %s AND %s "
        "ORDER BY game_date_et, tipoff_utc NULLS LAST, game_id",
        (date_from, date_to),
    )


@st.cache_data(ttl=600)
def load_model_status() -> dict:
    df = q(
        "SELECT model_name, model_version, trained_rows, metrics, created_at "
        "FROM public.model_registry_v2 WHERE is_active",
    )
    out = {}
    for _, r in df.iterrows():
        metrics = r["metrics"] if isinstance(r["metrics"], dict) else {}
        out[r["model_name"]] = {
            "version": r["model_version"],
            "trained_rows": r["trained_rows"],
            "metrics": metrics,
            "created_at": r["created_at"],
        }
    return out


@st.cache_data(ttl=600)
def load_pick_record(limit_days: int = 60) -> pd.DataFrame:
    return q(
        """
        SELECT p.game_id, g.game_date_et, g.home_abbr, g.away_abbr,
               p.pick_side, p.home_spread_used, p.home_price_used,
               p.away_price_used, p.cover_result, p.is_paper
        FROM public.predictions p
        JOIN public.games_v2 g ON g.game_id = p.game_id
        WHERE p.pick_side IS NOT NULL AND p.settled_at IS NOT NULL
          AND g.game_date_et >= %s
        ORDER BY g.game_date_et DESC
        """,
        (dt.date.today() - dt.timedelta(days=limit_days),),
    )


@st.cache_data(ttl=600)
def load_win_record(limit_days: int = 60) -> pd.DataFrame:
    """Straight-up winner-prediction track record: every settled game with a
    win probability (not just betting picks)."""
    return q(
        """
        SELECT p.game_id, g.game_date_et, g.home_abbr, g.away_abbr,
               p.p_home_win, p.win_result, p.is_paper
        FROM public.predictions p
        JOIN public.games_v2 g ON g.game_id = p.game_id
        WHERE p.p_home_win IS NOT NULL AND p.win_result IS NOT NULL
          AND p.settled_at IS NOT NULL AND g.game_date_et >= %s
        ORDER BY g.game_date_et DESC
        """,
        (dt.date.today() - dt.timedelta(days=limit_days),),
    )


@st.cache_data(ttl=600)
def load_team_recent(abbr: str, n: int = 10) -> pd.DataFrame:
    return q(
        """
        SELECT g.game_date_et, g.home_abbr, g.away_abbr, g.home_score,
               g.away_score, t.wl, t.pts, t.reb, t.ast, t.tov, t.plus_minus
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
        SELECT p.player_name AS 球員,
               count(*) AS 場次,
               round(avg(p.min_played)::numeric, 1) AS 分鐘,
               round(avg(p.pts)::numeric, 1) AS 得分,
               round(avg(p.reb)::numeric, 1) AS 籃板,
               round(avg(p.ast)::numeric, 1) AS 助攻,
               round(avg(p.plus_minus)::numeric, 1) AS 正負值
        FROM public.player_game_stats p
        JOIN public.games_v2 g ON g.game_id = p.game_id
        WHERE p.team_abbr = %s AND g.season = %s AND g.status = 'final'
        GROUP BY p.player_name
        HAVING avg(p.min_played) >= 10
        ORDER BY avg(p.pts) DESC
        """,
        (abbr, season),
    )


def insert_manual_line(game_id: str, spread: float, home_price: float,
                       away_price: float) -> None:
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.market_lines
              (game_id, source, book, home_spread, home_price, away_price)
            VALUES (%s, 'manual', NULL, %s, %s, %s)
            """,
            (game_id, spread, home_price, away_price),
        )
        conn.commit()


# =========================================================
# Closed-form recompute helpers (no sklearn pickles in the app)
# =========================================================

def calibrator_from_metrics(cal_metrics: dict) -> dict:
    """Rebuild the calibrator from registry metrics JSON. Isotonic lives only
    in the pickled payload, so the app approximates it with the Platt params
    recorded alongside (a deliberate, conservative fallback)."""
    method = (cal_metrics or {}).get("method")
    if method in ("platt", "isotonic") and "platt_a" in (cal_metrics or {}):
        return {"type": "platt", "a": cal_metrics["platt_a"], "b": cal_metrics["platt_b"]}
    return {"type": "identity"}


def fmt_team(abbr: str) -> str:
    return f"{TEAM_NAME_CH.get(abbr, abbr)} {abbr}"


def fmt_tipoff_tw(ts) -> str:
    if ts is None or pd.isna(ts):
        return "—"
    return pd.Timestamp(ts).tz_convert(TW).strftime("%m/%d %H:%M 台北")


SIDE_LABEL = {"HOME": "主隊", "AWAY": "客隊"}
ABSTAIN_LABEL = {
    "no_line": "無盤口", "stale_line": "盤口過舊", "early_season": "季初樣本不足",
    "disagreement_guard": "與市場分歧過大", "injury_veto": "主力傷停否決",
    "below_threshold": "優勢不足", "no_prices": "缺賠率", "capacity": "額滿",
}


# =========================================================
# Page
# =========================================================

st.title("🏀 NBA Edge 數據預測系統 v2")

status = load_model_status()
margin_info = status.get(MODEL_NAME)
cal_info = status.get(CALIBRATOR_NAME)
sigma = float((margin_info or {}).get("metrics", {}).get("sigma") or 12.5)
calibrator = calibrator_from_metrics((cal_info or {}).get("metrics", {}))
min_edge = effective_min_edge((cal_info or {}).get("metrics"))

with st.sidebar:
    st.subheader("檢視範圍")
    base_date = st.date_input("比賽日（美東）", value=dt.datetime.now(ET).date())
    days = st.slider("往後天數", 0, 6, 1)
    if st.button("🔄 重新整理資料"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("🧠 模型狀態")
    if margin_info:
        m = margin_info["metrics"]
        rm = (m.get("report_margin") or {})
        st.caption(
            f"margin_model `{margin_info['version']}`（{m.get('model_kind', '?')}）\n\n"
            f"訓練樣本 {margin_info['trained_rows']}・walk-forward MAE "
            f"{rm.get('mae', float('nan')):.2f}・σ={sigma:.1f}"
        )
        if not m.get("gates_passed", True):
            st.warning("⚠️ 模型未通過驗收閘門，僅供觀察")
        rb = m.get("report_betting") or {}
        ats = rb.get("ats") or {}
        su = rb.get("straight_up") or {}
        # winner-prediction accuracy is line-independent — always show when present
        if su.get("winner_accuracy") is not None:
            st.caption(f"🎯 勝負預測（walk-forward 時間外驗證）：猜中率 "
                       f"{su['winner_accuracy'] * 100:.1f}%（{su.get('n', 0)} 場）")
        # ATS hit-rate/ROI needs historical lines; show only when available
        if ats.get("hit_rate") is not None:
            st.caption(
                f"💰 ATS 回測：過盤 {ats['hit_rate'] * 100:.1f}%・"
                f"ROI {ats['roi'] * 100:+.1f}%（{ats['n_graded']} 注，損益兩平 52.4%）"
            )
        elif rb:
            st.caption("💰 ATS 回測：尚無歷史盤口，無法評估過盤/ROI（需接入盤口資料）。")
    else:
        st.error("尚無啟用中的 margin_model（先跑 v2_backfill + v2_train）")
    if cal_info:
        cm = cal_info["metrics"]
        st.caption(f"校準器 `{cal_info['version']}`：{cm.get('method', 'identity')}"
                   f"（n={cm.get('n', 0)}）")
        if cm.get("alarm"):
            st.warning(f"⚠️ 校準警報：選注門檻提高至 {CONFIG.MIN_EDGE_ALARM_MODE:.0%}")
    st.caption(f"選注門檻 edge ≥ {min_edge:.0%}・每日上限 {CONFIG.MAX_PICKS_PER_DAY} 注")

board = load_board(base_date, base_date + dt.timedelta(days=days))

if board.empty:
    st.info("此區間沒有比賽。")
    st.stop()

if board["is_paper"].fillna(False).any():
    st.info("📝 Paper mode 觀察期：以下推薦僅為紀錄驗證用，非下注建議。")

# ---------- Top picks ----------
st.header(f"🔥 過盤推薦（model lean，最多 {CONFIG.MAX_PICKS_PER_DAY} 注/日）")
picks = board[board["pick_side"].notna()].copy()
if picks.empty:
    st.caption("目前沒有達到門檻的推薦——沒有價值就不出手，不湊單。")
else:
    picks = picks.sort_values("edge_prob", ascending=False)
    cols = st.columns(min(4, len(picks)))
    for i, (_, r) in enumerate(picks.iterrows()):
        side_abbr = r["home_abbr"] if r["pick_side"] == "HOME" else r["away_abbr"]
        edge_side = (r["edge_prob"] if r["pick_side"] == "HOME"
                     else (None if r["edge_prob"] is None else -r["edge_prob"]))
        with cols[i % len(cols)]:
            with st.container(border=True):
                st.subheader(f"精選 {i + 1}")
                st.write(f"**{fmt_team(r['away_abbr'])} @ {fmt_team(r['home_abbr'])}**")
                st.write(f"🎯 {SIDE_LABEL[r['pick_side']]} **{fmt_team(side_abbr)}** "
                         f"盤口 {r['home_spread_used']:+.1f}（主隊）")
                p = r["p_home_cover"] if r["pick_side"] == "HOME" else 1 - r["p_home_cover"]
                st.metric("過盤機率", f"{p * 100:.1f}%")
                st.caption(f"{fmt_tipoff_tw(r['tipoff_utc'])}・模型讓分差 "
                           f"{r['pred_margin']:+.1f}")

# ---------- Full board ----------
st.header("🎯 全部場次")
for date_et, day_games in board.groupby("game_date_et"):
    st.subheader(f"📅 {date_et}（美東比賽日）")
    cols = st.columns(3)
    for i, (_, g) in enumerate(day_games.iterrows()):
        with cols[i % 3]:
            with st.container(border=True):
                st.markdown(f"**{fmt_team(g['away_abbr'])} @ {fmt_team(g['home_abbr'])}**")
                st.caption(fmt_tipoff_tw(g["tipoff_utc"]))

                if g["status"] == "final":
                    st.write(f"🏁 終場 {g['away_score']} : {g['home_score']}")
                    if g["cover_result"] is not None and not pd.isna(g["cover_result"]):
                        res = {1: "主隊過盤", 0: "客隊過盤", 2: "Push"}.get(int(g["cover_result"]), "")
                        st.caption(f"結算（對下注時盤口 {g['home_spread_used']:+.1f}）：{res}")
                elif g["status"] == "live":
                    st.write(f"🔴 進行中 {g['away_score']} : {g['home_score']}")

                if pd.notna(g["home_spread"]):
                    src = "✍️ 手動" if g["line_source"] == "manual" else (g["book"] or g["line_source"] or "")
                    price_txt = ""
                    if pd.notna(g["home_price"]) and pd.notna(g["away_price"]):
                        price_txt = f"・{g['home_price']:.2f} / {g['away_price']:.2f}"
                    st.write(f"盤口（主隊）**{g['home_spread']:+.1f}**{price_txt}　`{src}`")
                else:
                    st.write("盤口：—")

                if pd.notna(g.get("p_home_win")):
                    pw = float(g["p_home_win"])
                    fav_abbr, fav_p = (g["home_abbr"], pw) if pw >= 0.5 else (g["away_abbr"], 1 - pw)
                    st.write(f"🏆 勝負預測：**{fmt_team(fav_abbr)}** 勝率 {fav_p * 100:.0f}%"
                             f"（主 {pw * 100:.0f}% / 客 {(1 - pw) * 100:.0f}%）")

                if pd.notna(g["pred_margin"]):
                    p_txt = (f"・主隊過盤 {g['p_home_cover'] * 100:.1f}%"
                             if pd.notna(g["p_home_cover"]) else "")
                    e_txt = (f"・edge {g['edge_prob'] * 100:+.1f}%"
                             if pd.notna(g["edge_prob"]) else "")
                    st.write(f"模型讓分差 **{g['pred_margin']:+.1f}**{p_txt}{e_txt}")
                    if g["pick_side"]:
                        st.success(f"建議：{SIDE_LABEL[g['pick_side']]}")
                    elif g["abstain_reason"]:
                        st.caption(f"不出手：{ABSTAIN_LABEL.get(g['abstain_reason'], g['abstain_reason'])}")

                # manual line entry + closed-form instant recompute
                if g["status"] == "scheduled":
                    with st.expander("✍️ 手動輸入盤口"):
                        ms = st.number_input("主隊讓分", value=float(g["home_spread"])
                                             if pd.notna(g["home_spread"]) else 0.0,
                                             step=0.5, key=f"ms_{g['game_id']}")
                        c1, c2 = st.columns(2)
                        mh = c1.number_input("主隊賠率", value=1.91, step=0.01,
                                             key=f"mh_{g['game_id']}")
                        ma = c2.number_input("客隊賠率", value=1.91, step=0.01,
                                             key=f"ma_{g['game_id']}")
                        if pd.notna(g["pred_margin"]):
                            d = decide_game(float(g["pred_margin"]), sigma,
                                            home_spread=ms, home_price=mh, away_price=ma,
                                            calibrator=calibrator, min_edge=min_edge,
                                            games_played_min=99)
                            st.caption(f"試算：主隊過盤 {d['p_cal'] * 100:.1f}%・"
                                       f"edge 主 {d['edge_home'] * 100:+.1f}% / "
                                       f"客 {d['edge_away'] * 100:+.1f}%")
                        if st.button("儲存盤口", key=f"save_{g['game_id']}"):
                            insert_manual_line(g["game_id"], float(ms), float(mh), float(ma))
                            st.cache_data.clear()
                            st.rerun()

# ---------- Pick record ----------
st.header("📊 已結算推薦戰績（近 60 天）")
rec = load_pick_record()
if rec.empty:
    st.caption("尚無已結算的推薦。")
else:
    graded = rec[rec["cover_result"].isin([0, 1])].copy()
    if not graded.empty:
        graded["win"] = ((graded["pick_side"] == "HOME") & (graded["cover_result"] == 1)) | \
                        ((graded["pick_side"] == "AWAY") & (graded["cover_result"] == 0))
        price = graded.apply(
            lambda r: r["home_price_used"] if r["pick_side"] == "HOME" else r["away_price_used"],
            axis=1).fillna(1.91)
        pnl = (graded["win"] * (price - 1) - (~graded["win"]) * 1.0).sum()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("注數", len(graded))
        c2.metric("命中率", f"{graded['win'].mean() * 100:.1f}%")
        c3.metric("平注損益", f"{pnl:+.2f} u")
        c4.metric("Push", int((rec["cover_result"] == 2).sum()))
        st.caption("打和（push）退款不計；損益以下注時賠率平注計算。52.4% 為 -110 盤的損益兩平線。")
    show = rec.copy()
    show["對戰"] = show.apply(lambda r: f"{r['away_abbr']} @ {r['home_abbr']}", axis=1)
    show["結果"] = show["cover_result"].map({1: "主過盤", 0: "客過盤", 2: "Push"})
    st.dataframe(show[["game_date_et", "對戰", "pick_side", "home_spread_used",
                       "結果", "is_paper"]], use_container_width=True, hide_index=True)

# ---------- Winner-prediction record ----------
st.header("🏆 勝負預測命中率（近 60 天，每場皆計）")
try:
    wr = load_win_record()
except Exception:
    wr = pd.DataFrame()  # columns appear only after schema migration runs
if wr.empty:
    st.caption("尚無已結算的勝負預測。")
else:
    wr["pred_home"] = wr["p_home_win"] >= 0.5
    wr["actual_home"] = wr["win_result"] == 1
    wr["correct"] = wr["pred_home"] == wr["actual_home"]
    c1, c2, c3 = st.columns(3)
    c1.metric("場次", len(wr))
    c2.metric("猜中率", f"{wr['correct'].mean() * 100:.1f}%")
    # confidence buckets: how good are the high-confidence calls?
    conf = wr["p_home_win"].where(wr["pred_home"], 1 - wr["p_home_win"])
    high = wr[conf >= 0.65]
    c3.metric("高信心(≥65%)猜中率",
              f"{high['correct'].mean() * 100:.1f}%" if not high.empty else "—")
    st.caption("此為直接預測贏家的命中率（非過盤）。高命中率不等於賺錢——賠率已反映強弱；"
               "能否獲利看上方 ATS 的 ROI。")

# ---------- Deep dive ----------
st.header("🔍 深度數據查詢")
season_guess = board["season"].iloc[0]
team_pick = st.selectbox("選擇隊伍", sorted(TEAM_NAME_CH),
                         format_func=lambda a: f"{TEAM_NAME_CH[a]} {a}")
c1, c2 = st.columns(2)
with c1:
    st.subheader(f"{TEAM_NAME_CH[team_pick]} 近 10 場")
    st.dataframe(load_team_recent(team_pick), use_container_width=True, hide_index=True)
with c2:
    st.subheader(f"{TEAM_NAME_CH[team_pick]} 球員場均（{season_guess}）")
    st.dataframe(load_player_recent(team_pick, season_guess),
                 use_container_width=True, hide_index=True)

with st.expander("ℹ️ 系統流程說明"):
    st.markdown(f"""
1. **每日管線**（GitHub Actions）：NBA CDN 賽程 → leaguegamelog 數據 → 結算 → 傷兵/盤口快照 → 特徵 → 預測。
2. **模型**：margin 回歸（walk-forward 驗證）→ 常態殘差轉過盤機率（σ={sigma:.1f}）→ 前向校準。
3. **選注**：對去抽水後的公平機率算 edge，需 edge ≥ {min_edge:.0%} 且 EV > 0；不湊單、不接過舊盤口。
4. **本頁**：純讀取 `v_app_board`；唯一寫入是手動盤口（下次預測自動採用，優先於書商盤口）。
""")