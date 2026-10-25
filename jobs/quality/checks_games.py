"""A (games) and C (starters, officials) checks. SELECT only.

Every query starts with a /* qc:<ID> */ marker so tests can route it, and returns one
row per (season, season_type) with n_total / n_bad counts; Findings are built in Python.
"""
from __future__ import annotations

from typing import Callable, List

import pandas as pd

from jobs.quality.core import Finding, Scope, grouped_check, scope_where

Run = Callable[[str, tuple], pd.DataFrame]

# ---- A2: regular-season games per team --------------------------------------------------
EXPECTED_GAMES = {"2020-21": 72}          # every other checked season: 82
EXPECTED_TOTAL = {"2020-21": 1080}        # league total used to detect a season still in progress
SKIP_SEASONS = {"2019-20"}                # suspended and finished with a reduced schedule
CUP_SEASONS_FROM = "2023-24"              # NBA Cup final is tagged 'regular': two teams reach 83

_A2 = """/* qc:A2 */
WITH tg AS (
  SELECT g.season, g.home_abbr AS team_abbr FROM public.games_v2 g
   WHERE g.season_type = 'regular' AND g.status = 'final'{extra}
  UNION ALL
  SELECT g.season, g.away_abbr FROM public.games_v2 g
   WHERE g.season_type = 'regular' AND g.status = 'final'{extra}
)
SELECT season, team_abbr, count(*) AS n FROM tg GROUP BY season, team_abbr ORDER BY season, team_abbr
"""


def check_a2_team_game_counts(run: Run, scope: Scope) -> List[Finding]:
    if scope.since is not None:           # season totals mean nothing for a recent window
        return [Finding("A2", "warn", "all/none", 0, 0, {})]
    extra, params = scope_where(scope)
    df = run(_A2.format(extra=extra), params * 2)
    out: List[Finding] = []
    for season, grp in df.groupby("season") if not df.empty else []:
        if season in SKIP_SEASONS:
            continue
        expected = EXPECTED_GAMES.get(season, 82)
        if grp["n"].sum() / 2 < 0.95 * EXPECTED_TOTAL.get(season, 1230):
            continue                       # season still in progress
        counts = grp["n"].astype(int).tolist()
        cup = season >= CUP_SEASONS_FROM
        ok = (lambda n: n in (expected, expected + 1)) if cup else (lambda n: n == expected)
        out.append(Finding("A2", "warn", f"{season}/regular", sum(1 for n in counts if not ok(n)),
                           len(counts), {"expected": expected}))
        if cup:
            n83 = sum(1 for n in counts if n == expected + 1)
            out.append(Finding("A2", "error", f"{season}/regular-83", 0 if n83 == 2 else 1, 1,
                               {"teams_with_extra_game": n83}))
    return out or [Finding("A2", "warn", "all/none", 0, 0, {})]


# ---- one-row-per-(season, season_type) checks -------------------------------------------
_A3 = """/* qc:A3 */
WITH d AS (
  SELECT g.season, g.season_type, g.game_date_et, g.home_abbr, g.away_abbr, count(*) AS c
    FROM public.games_v2 g WHERE 1=1{extra}
   GROUP BY g.season, g.season_type, g.game_date_et, g.home_abbr, g.away_abbr)
SELECT season, season_type, count(*) AS n_total, count(*) FILTER (WHERE c > 1) AS n_bad
  FROM d GROUP BY season, season_type ORDER BY season, season_type
"""

_A4 = """/* qc:A4 */
WITH t AS (
  SELECT g.season, g.season_type, g.game_date_et AS d, g.home_abbr AS team FROM public.games_v2 g WHERE 1=1{extra}
  UNION ALL
  SELECT g.season, g.season_type, g.game_date_et, g.away_abbr FROM public.games_v2 g WHERE 1=1{extra}
), per_day AS (
  SELECT season, season_type, d, team, count(*) AS c FROM t GROUP BY season, season_type, d, team)
SELECT season, season_type, count(*) AS n_total, count(*) FILTER (WHERE c > 1) AS n_bad
  FROM per_day GROUP BY season, season_type ORDER BY season, season_type
"""

_A5 = """/* qc:A5 */
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE g.status <> 'final' AND g.game_date_et < current_date - 3) AS n_bad
  FROM public.games_v2 g WHERE 1=1{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_A6 = """/* qc:A6 */
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE g.margin IS NULL OR g.home_score IS NULL OR g.away_score IS NULL
                           OR g.margin = 0) AS n_bad
  FROM public.games_v2 g WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_A7 = """/* qc:A7 */
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE g.tipoff_utc IS NULL
                           OR extract(hour FROM g.tipoff_utc AT TIME ZONE 'America/New_York') < 10
                           OR (g.tipoff_utc AT TIME ZONE 'America/New_York')::date <> g.game_date_et) AS n_bad,
       count(*) FILTER (WHERE extract(hour FROM g.tipoff_utc AT TIME ZONE 'America/New_York') < 10) AS early_hour,
       count(*) FILTER (WHERE g.tipoff_utc IS NULL) AS tip_null,
       count(*) FILTER (WHERE (g.tipoff_utc AT TIME ZONE 'America/New_York')::date <> g.game_date_et) AS date_mismatch
  FROM public.games_v2 g WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_C1 = """/* qc:C1 */
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE coalesce(s.n, 0) = 0) AS n_bad
  FROM public.games_v2 g
  LEFT JOIN (SELECT game_id, count(*) AS n FROM public.player_game_status GROUP BY game_id) s
         ON s.game_id = g.game_id
 WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_C2 = """/* qc:C2 */
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE s.n_start <> 10) AS n_bad
  FROM public.games_v2 g
  JOIN (SELECT game_id, count(*) FILTER (WHERE starter) AS n_start
          FROM public.player_game_status GROUP BY game_id) s ON s.game_id = g.game_id
 WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_C3 = """/* qc:C3 */
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE coalesce(o.n, 0) < 3) AS n_bad
  FROM public.games_v2 g
  LEFT JOIN (SELECT game_id, count(*) AS n FROM public.game_officials GROUP BY game_id) o
         ON o.game_id = g.game_id
 WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""


check_a3_duplicate_games = grouped_check("A3", "error", _A3)
check_a4_team_twice_same_day = grouped_check("A4", "warn", _A4, repeat=2)
check_a5_stuck_not_final = grouped_check("A5", "error", _A5)
check_a6_final_without_score = grouped_check("A6", "error", _A6)
check_a7_suspicious_tipoff = grouped_check("A7", "warn", _A7, ("early_hour", "tip_null", "date_mismatch"))
check_c1_status_rows_present = grouped_check("C1", "error", _C1)
check_c2_starters_are_ten = grouped_check("C2", "warn", _C2)
check_c3_officials_at_least_three = grouped_check("C3", "info", _C3)
