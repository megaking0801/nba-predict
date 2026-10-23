"""Read-only SQL for the NBA prediction site.

Every function takes ``run(sql, params) -> DataFrame`` so the Streamlit app
(cached ``q``) and the static builder (one read-only connection) share the
same queries. SELECT only — enforced by tests/test_webui_queries.py.
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from jobs.model import MODEL_NAME

Run = Callable[[str, tuple], pd.DataFrame]


def recent_games(run: Run, n: int = 30) -> pd.DataFrame:
    """Newest predicted games — upcoming first when in season, else recent finals."""
    return run(
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


def track_record(run: Run) -> dict:
    """Headline accuracy: stored walk-forward number + live settled record."""
    df = run("SELECT metrics FROM public.model_registry_v2 "
             "WHERE model_name=%s AND is_active", (MODEL_NAME,))
    wf = {}
    if not df.empty:
        m = df.iloc[0]["metrics"] if isinstance(df.iloc[0]["metrics"], dict) else {}
        wf = (m.get("report_betting") or {}).get("straight_up") or {}
    # One prediction per game (a game is predicted up to 3 days running): prefer the
    # last one made before tipoff, else the latest. A preference, not a filter, so
    # backfilled predictions dated after tipoff still count.
    rec = run("WITH last AS ("
              "SELECT DISTINCT ON (p.game_id) p.p_home_win, p.win_result "
              "FROM public.predictions p JOIN public.games_v2 g ON g.game_id = p.game_id "
              "WHERE p.p_home_win IS NOT NULL AND p.win_result IS NOT NULL "
              "ORDER BY p.game_id, (p.predicted_at < g.tipoff_utc) DESC NULLS LAST, "
              "p.predicted_at DESC) "
              "SELECT count(*) FILTER (WHERE (p_home_win>=0.5)=(win_result=1)) AS hit, "
              "count(*) AS n FROM last", ())

    # totals (大小分): walk-forward O/U accuracy + live settled O/U record
    tdf = run("SELECT metrics FROM public.model_registry_v2 "
              "WHERE model_name=%s AND is_active", ("total_model",))
    over = {}
    if not tdf.empty:
        tm = tdf.iloc[0]["metrics"] if isinstance(tdf.iloc[0]["metrics"], dict) else {}
        over = (tm.get("report_over") or {}) if isinstance(tm, dict) else {}
    reco = run("WITH last AS ("
               "SELECT DISTINCT ON (p.game_id) p.p_over, p.over_result "
               "FROM public.predictions p JOIN public.games_v2 g ON g.game_id = p.game_id "
               "WHERE p.p_over IS NOT NULL AND p.over_result IS NOT NULL "
               "ORDER BY p.game_id, (p.predicted_at < g.tipoff_utc) DESC NULLS LAST, "
               "p.predicted_at DESC) "
               "SELECT count(*) FILTER (WHERE (p_over>=0.5)=(over_result=1)) AS hit, "
               "count(*) AS n FROM last WHERE over_result IN (0,1)", ())
    return {"wf_acc": wf.get("winner_accuracy"), "wf_n": wf.get("n"),
            "rec_hit": int(rec.iloc[0]["hit"] or 0), "rec_n": int(rec.iloc[0]["n"] or 0),
            "over_wf_acc": over.get("accuracy"), "over_wf_n": over.get("n"),
            "over_rec_hit": int(reco.iloc[0]["hit"] or 0),
            "over_rec_n": int(reco.iloc[0]["n"] or 0)}


def data_freshness(run: Run):
    """Newest prediction time (tz-aware UTC), or None when there are no predictions."""
    df = run("SELECT max(predicted_at) AS predicted_at FROM public.predictions", ())
    if df.empty or pd.isna(df.iloc[0]["predicted_at"]):
        return None
    ts = pd.Timestamp(df.iloc[0]["predicted_at"])
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.to_pydatetime()


def next_tipoff(run: Run):
    """Earliest future tipoff of a scheduled game (tz-aware UTC), or None."""
    df = run("SELECT min(tipoff_utc) AS tipoff_utc FROM public.games_v2 "
             "WHERE status='scheduled' AND tipoff_utc > now()", ())
    if df.empty or pd.isna(df.iloc[0]["tipoff_utc"]):
        return None
    ts = pd.Timestamp(df.iloc[0]["tipoff_utc"])
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.to_pydatetime()


def team_recent(run: Run, abbr: str, n: int = 10) -> pd.DataFrame:
    return run(
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


def player_recent(run: Run, abbr: str, season: str) -> pd.DataFrame:
    return run(
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


def latest_season(run: Run) -> str:
    df = run("SELECT season FROM public.games_v2 ORDER BY season DESC LIMIT 1", ())
    return df.iloc[0]["season"] if not df.empty else "2025-26"


def game(run: Run, gid: str) -> pd.DataFrame:
    """Full board row for one game — drives the matchup detail page."""
    return run("SELECT * FROM public.v_app_board WHERE game_id = %s", (gid,))


def injuries(run: Run, team_abbr: str, ref_date) -> pd.DataFrame:
    """Injury snapshot for a team: latest on/before ref_date, else most recent
    available (so past games still show current injuries). Out listed first.
    Includes snap_date so the UI can show which day the report is from."""
    return run(
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
