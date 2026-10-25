"""Per-game quality flags (read-only).

``game_quality()`` runs ONE SELECT and returns a row per game with the flags in the
design (section 5) plus the raw counts they are computed from (``raw_*``).
``derive_flags()`` recomputes every flag from the raw counts in plain Python and
``verify_flags()`` compares the two, which is the only way to check the SQL: run it
against the real database (``python -m jobs.quality --verify-flags``).
"""
from __future__ import annotations

import math
from datetime import date
from typing import Callable, List
from zoneinfo import ZoneInfo

import pandas as pd

from jobs.quality.core import Scope, scope_where

Run = Callable[[str, tuple], pd.DataFrame]

CUP_FINAL_IDS = {"401607495", "401734908", "401809839"}   # NBA Cup finals (tagged 'regular' by ESPN)
BUBBLE_START, BUBBLE_END = date(2020, 7, 30), date(2020, 10, 12)
CROWD_RESTRICTED = (date(2020, 12, 22), date(2021, 5, 31))   # date approximation, no attendance data
SPREAD_MAX = 25
TOTAL_RANGE = (170, 280)
ET = ZoneInfo("America/New_York")

RAW_COLUMNS = [
    "raw_status", "raw_team_rows", "raw_team_nonnull_n", "raw_player_min_nonnull_min",
    "raw_player_pts_diff", "raw_box_fga_nonnull", "raw_bucket_diff_max", "raw_bucket_rows",
    "raw_status_rows", "raw_starters_n", "raw_officials_n", "raw_spread", "raw_total",
    "raw_line_price_point", "raw_line_total_price_point", "raw_line_captured_at", "raw_tipoff_utc",
]
FLAG_COLUMNS = [
    "team_box_ok", "player_box_ok", "points_consistent", "shots_class", "shots_diff_max", "status_ok",
    "officials_n", "has_spread", "spread_ok", "has_total", "total_ok", "spread_provenance",
    "total_provenance", "is_cup_final", "is_bubble", "crowd_restricted", "suspicious_tipoff",
    "usable_team_features", "usable_player_features", "usable_shot_features", "usable_spread_line",
    "usable_total_line",
]
ID_COLUMNS = ["game_id", "season", "season_type", "game_date_et"]
GAME_QUALITY_COLUMNS = ID_COLUMNS + FLAG_COLUMNS + RAW_COLUMNS

SQL = """/* qc:GQ */
WITH tg AS (
  SELECT t.game_id, count(*) AS team_rows,
         count(*) FILTER (WHERE t.fga IS NOT NULL AND t.fgm IS NOT NULL AND t.fg3a IS NOT NULL
                             AND t.fta IS NOT NULL AND t.tov IS NOT NULL AND t.oreb IS NOT NULL
                             AND t.dreb IS NOT NULL) AS team_nonnull_n,
         bool_and(t.fga IS NOT NULL) AS box_fga_nonnull
    FROM public.team_game_stats t GROUP BY t.game_id
), pl AS (
  SELECT p.game_id, p.team_abbr, count(p.min_played) AS n_min, sum(p.pts) AS pp
    FROM public.player_game_stats p GROUP BY p.game_id, p.team_abbr
), plg AS (
  SELECT pl.game_id, count(*) AS pl_teams, min(pl.n_min) AS min_n_min,
         CASE WHEN count(*) FILTER (WHERE t.pts IS NULL OR pl.pp IS NULL) > 0 THEN NULL
              ELSE sum(abs(pl.pp - t.pts)) END AS pts_diff
    FROM pl JOIN public.team_game_stats t ON t.game_id = pl.game_id AND t.team_abbr = pl.team_abbr
   GROUP BY pl.game_id
), sh AS (
  SELECT s.game_id, s.team_abbr, sum(s.fga) AS pfga
    FROM public.team_shot_buckets s GROUP BY s.game_id, s.team_abbr
), shg AS (
  SELECT sh.game_id, count(*) AS bucket_teams, max(abs(sh.pfga - t.fga)) AS diff_max
    FROM sh JOIN public.team_game_stats t ON t.game_id = sh.game_id AND t.team_abbr = sh.team_abbr
   GROUP BY sh.game_id
), st AS (
  SELECT game_id, count(*) AS n, count(*) FILTER (WHERE starter) AS n_start
    FROM public.player_game_status GROUP BY game_id
), ofc AS (
  SELECT game_id, count(*) AS n FROM public.game_officials GROUP BY game_id
), base AS (
  SELECT g.game_id, g.season, g.season_type, g.game_date_et,
         g.status AS raw_status,
         coalesce(tg.team_rows, 0) AS raw_team_rows,
         coalesce(tg.team_nonnull_n, 0) AS raw_team_nonnull_n,
         CASE WHEN plg.pl_teams = 2 THEN plg.min_n_min ELSE 0 END AS raw_player_min_nonnull_min,
         plg.pts_diff AS raw_player_pts_diff,
         coalesce(tg.box_fga_nonnull AND tg.team_rows = 2, false) AS raw_box_fga_nonnull,
         CASE WHEN shg.bucket_teams = 2 THEN shg.diff_max END AS raw_bucket_diff_max,
         coalesce(shg.bucket_teams, 0) AS raw_bucket_rows,
         coalesce(st.n, 0) AS raw_status_rows,
         coalesce(st.n_start, 0) AS raw_starters_n,
         coalesce(ofc.n, 0) AS raw_officials_n,
         l.home_spread AS raw_spread,
         l.total_line AS raw_total,
         l.price_point AS raw_line_price_point,
         l.total_price_point AS raw_line_total_price_point,
         l.captured_at AS raw_line_captured_at,
         g.tipoff_utc AS raw_tipoff_utc
    FROM public.games_v2 g
    LEFT JOIN tg ON tg.game_id = g.game_id
    LEFT JOIN plg ON plg.game_id = g.game_id
    LEFT JOIN shg ON shg.game_id = g.game_id
    LEFT JOIN st ON st.game_id = g.game_id
    LEFT JOIN ofc ON ofc.game_id = g.game_id
    LEFT JOIN public.v_closing_lines l ON l.game_id = g.game_id
   WHERE 1=1{extra}
), f AS (
  SELECT base.*,
         (raw_team_rows = 2 AND raw_team_nonnull_n = 2) AS team_box_ok,
         (raw_player_min_nonnull_min > 0) AS player_box_ok,
         CASE WHEN raw_player_min_nonnull_min > 0 THEN raw_player_pts_diff = 0 END AS points_consistent,
         CASE WHEN NOT raw_box_fga_nonnull THEN 'no_box'
              WHEN raw_bucket_diff_max IS NULL THEN 'none'
              WHEN raw_bucket_diff_max = 0 THEN 'exact'
              WHEN raw_bucket_diff_max <= 2 THEN 'minor' ELSE 'major' END AS shots_class,
         raw_bucket_diff_max AS shots_diff_max,
         (raw_status_rows > 0 AND raw_starters_n = 10) AS status_ok,
         raw_officials_n AS officials_n,
         (raw_spread IS NOT NULL) AS has_spread,
         (raw_spread IS NOT NULL AND abs(raw_spread) <= 25) AS spread_ok,
         (raw_total IS NOT NULL) AS has_total,
         (raw_total IS NOT NULL AND raw_total BETWEEN 170 AND 280) AS total_ok,
         CASE WHEN raw_spread IS NULL THEN 'none'
              WHEN raw_line_price_point IS NOT NULL THEN raw_line_price_point
              WHEN raw_line_captured_at = raw_tipoff_utc THEN 'backfill_unlabeled'
              ELSE 'unlabeled' END AS spread_provenance,
         CASE WHEN raw_total IS NULL THEN 'none'
              WHEN raw_line_total_price_point IS NOT NULL THEN raw_line_total_price_point
              WHEN raw_line_captured_at = raw_tipoff_utc THEN 'backfill_unlabeled'
              ELSE 'unlabeled' END AS total_provenance,
         (game_id IN ('401607495', '401734908', '401809839')) AS is_cup_final,
         (game_date_et BETWEEN DATE '2020-07-30' AND DATE '2020-10-12') AS is_bubble,
         (game_date_et BETWEEN DATE '2020-12-22' AND DATE '2021-05-31') AS crowd_restricted,
         (raw_tipoff_utc IS NULL
            OR extract(hour FROM raw_tipoff_utc AT TIME ZONE 'America/New_York') < 10
            OR (raw_tipoff_utc AT TIME ZONE 'America/New_York')::date <> game_date_et) AS suspicious_tipoff
    FROM base
)
SELECT game_id, season, season_type, game_date_et,
       team_box_ok, player_box_ok, points_consistent, shots_class, shots_diff_max, status_ok,
       officials_n, has_spread, spread_ok, has_total, total_ok, spread_provenance, total_provenance,
       is_cup_final, is_bubble, crowd_restricted, suspicious_tipoff,
       (raw_status = 'final' AND team_box_ok) AS usable_team_features,
       (raw_status = 'final' AND team_box_ok AND player_box_ok AND coalesce(points_consistent, false))
         AS usable_player_features,
       (shots_class IN ('exact', 'minor')) AS usable_shot_features,
       (has_spread AND spread_ok) AS usable_spread_line,
       (has_total AND total_ok) AS usable_total_line,
       raw_status, raw_team_rows, raw_team_nonnull_n, raw_player_min_nonnull_min, raw_player_pts_diff,
       raw_box_fga_nonnull, raw_bucket_diff_max, raw_bucket_rows, raw_status_rows, raw_starters_n,
       raw_officials_n, raw_spread, raw_total, raw_line_price_point, raw_line_total_price_point,
       raw_line_captured_at, raw_tipoff_utc
  FROM f ORDER BY game_date_et, game_id
"""


def game_quality(run: Run, scope: Scope) -> pd.DataFrame:
    extra, params = scope_where(scope)
    return run(SQL.format(extra=extra), params)


def _isnull(v) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _provenance(has_line: bool, point, row: dict) -> str:
    if not has_line:
        return "none"
    if not _isnull(point):
        return point
    return ("backfill_unlabeled" if row["raw_line_captured_at"] == row["raw_tipoff_utc"] else "unlabeled")


def derive_flags(row: dict) -> dict:
    """The same flags as the SELECT, computed from its raw_* columns (plus the id columns)."""
    g = lambda k: (None if _isnull(row.get(k)) else row.get(k))
    f: dict = {}
    f["team_box_ok"] = g("raw_team_rows") == 2 and g("raw_team_nonnull_n") == 2
    f["player_box_ok"] = (g("raw_player_min_nonnull_min") or 0) > 0
    f["points_consistent"] = ((g("raw_player_pts_diff") == 0)
                              if f["player_box_ok"] and g("raw_player_pts_diff") is not None else None)
    diff = g("raw_bucket_diff_max")
    if not g("raw_box_fga_nonnull"):
        f["shots_class"] = "no_box"
    elif diff is None:
        f["shots_class"] = "none"
    else:
        f["shots_class"] = "exact" if diff == 0 else ("minor" if diff <= 2 else "major")
    f["shots_diff_max"] = diff
    f["status_ok"] = (g("raw_status_rows") or 0) > 0 and g("raw_starters_n") == 10
    f["officials_n"] = g("raw_officials_n") or 0
    spread, total = g("raw_spread"), g("raw_total")
    f["has_spread"] = spread is not None
    f["spread_ok"] = spread is not None and abs(spread) <= SPREAD_MAX
    f["has_total"] = total is not None
    f["total_ok"] = total is not None and TOTAL_RANGE[0] <= total <= TOTAL_RANGE[1]
    f["spread_provenance"] = _provenance(f["has_spread"], g("raw_line_price_point"), row)
    f["total_provenance"] = _provenance(f["has_total"], g("raw_line_total_price_point"), row)
    d = row["game_date_et"]
    if hasattr(d, "date") and callable(d.date):      # pandas Timestamp / datetime -> date
        d = d.date()
    f["is_cup_final"] = str(row["game_id"]) in CUP_FINAL_IDS
    f["is_bubble"] = BUBBLE_START <= d <= BUBBLE_END
    f["crowd_restricted"] = CROWD_RESTRICTED[0] <= d <= CROWD_RESTRICTED[1]
    tip = g("raw_tipoff_utc")
    if tip is None:
        f["suspicious_tipoff"] = True
    else:
        ts = pd.Timestamp(tip)
        local = (ts.tz_localize("UTC") if ts.tzinfo is None else ts).tz_convert(ET)
        f["suspicious_tipoff"] = bool(local.hour < 10 or local.date() != d)
    final = g("raw_status") == "final"
    f["usable_team_features"] = final and f["team_box_ok"]
    f["usable_player_features"] = (final and f["team_box_ok"] and f["player_box_ok"]
                                   and bool(f["points_consistent"]))
    f["usable_shot_features"] = f["shots_class"] in ("exact", "minor")
    f["usable_spread_line"] = f["has_spread"] and f["spread_ok"]
    f["usable_total_line"] = f["has_total"] and f["total_ok"]
    return f


def _norm(v):
    if _isnull(v):
        return None
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        v = v.item()
    if isinstance(v, float) and math.isfinite(v) and v == int(v):
        return int(v)
    return v


def verify_flags(df: pd.DataFrame) -> List[str]:
    """game_ids whose SQL flags differ from derive_flags() on the same raw counts."""
    bad: List[str] = []
    for _, r in df.iterrows():
        row = r.to_dict()
        want = derive_flags(row)
        if any(_norm(row.get(k)) != _norm(want[k]) for k in want):
            bad.append(str(row["game_id"]))
    return bad
