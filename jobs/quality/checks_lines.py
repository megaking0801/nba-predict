"""E (betting line) checks. SELECT only.

The line for a game is always the one in public.v_closing_lines (the project's
closing-line rule); ratio-type checks only judge a season once it has enough games.
"""
from __future__ import annotations

from typing import Callable, List

import pandas as pd

from jobs.quality.core import Finding, Scope, grouped_check, scope_where

Run = Callable[[str, tuple], pd.DataFrame]
MIN_GAMES = 200
SPREAD_CORR = (0.30, 0.65)
SPREAD_MAE = (8.0, 12.0)
SPREAD_BIAS = 1.0
TOTAL_RANGE = (170, 280)
TOTAL_CORR_MIN = 0.25
TOTAL_ERR_MAX = 2.0

_E1 = """/* qc:E1 */
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM public.market_lines ml
                                           WHERE ml.game_id = g.game_id AND ml.source = 'espn')) AS n_bad
  FROM public.games_v2 g
 WHERE g.status = 'final' AND g.season_type IN ('regular', 'playoffs', 'playin'){extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_E2 = """/* qc:E2 */
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE l.home_spread IS NULL OR abs(l.home_spread) > 25) AS n_bad
  FROM public.games_v2 g
  JOIN public.v_closing_lines l ON l.game_id = g.game_id
 WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_E3 = """/* qc:E3 */
SELECT g.season, count(*) AS n,
       corr(g.margin, -l.home_spread) AS corr_v,
       avg(abs(g.margin + l.home_spread)) AS mae,
       avg(g.margin + l.home_spread) AS bias
  FROM public.games_v2 g
  JOIN public.v_closing_lines l ON l.game_id = g.game_id
 WHERE g.status = 'final' AND g.season_type = 'regular'{extra}
 GROUP BY g.season ORDER BY g.season
"""

_E4 = """/* qc:E4 */
SELECT g.season, g.season_type,
       count(*) FILTER (WHERE l.total_line IS NOT NULL) AS n_total,
       count(*) FILTER (WHERE l.total_line IS NOT NULL
                           AND (l.total_line < 170 OR l.total_line > 280)) AS n_bad,
       count(*) FILTER (WHERE l.total_line IS NULL) AS null_total
  FROM public.games_v2 g
  JOIN public.v_closing_lines l ON l.game_id = g.game_id
 WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_E5 = """/* qc:E5 */
SELECT g.season, count(*) AS n,
       corr(g.home_score + g.away_score, l.total_line) AS corr_total,
       avg((g.home_score + g.away_score) - l.total_line) AS mean_err
  FROM public.games_v2 g
  JOIN public.v_closing_lines l ON l.game_id = g.game_id
 WHERE g.status = 'final' AND g.season_type = 'regular'
   AND l.total_line BETWEEN 170 AND 280{extra}
 GROUP BY g.season ORDER BY g.season
"""

_E6 = """/* qc:E6 */
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE ml.price_point IS NULL AND ml.captured_at <> g.tipoff_utc) AS n_bad,
       count(*) FILTER (WHERE ml.price_point IS NULL) AS n_null,
       count(*) FILTER (WHERE ml.price_point = 'close') AS n_close,
       count(*) FILTER (WHERE ml.price_point = 'current') AS n_current,
       count(*) FILTER (WHERE ml.price_point = 'open') AS n_open
  FROM public.market_lines ml
  JOIN public.games_v2 g ON g.game_id = ml.game_id
 WHERE ml.source = 'espn'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_E7 = """/* qc:E7 */
SELECT g.season, count(*) AS n, corr(g.margin, -l.home_spread) AS corr_v
  FROM public.games_v2 g
  JOIN public.v_closing_lines l ON l.game_id = g.game_id
 WHERE g.status = 'final' AND g.season_type = 'regular'{extra}
 GROUP BY g.season ORDER BY g.season
"""

check_e1_line_coverage = grouped_check("E1", "warn", _E1)
check_e2_spread_range = grouped_check("E2", "error", _E2)
check_e4_total_range = grouped_check("E4", "error", _E4, ("null_total",))
check_e6_price_point_provenance = grouped_check(
    "E6", "warn", _E6, ("n_null", "n_close", "n_current", "n_open"))


def _num(v):
    return None if v is None or pd.isna(v) else float(v)


def _per_season(check_id: str, severity: str, sql: str, judge: Callable[[dict], int],
                n_metrics: int) -> Callable:
    """Ratio-type checks: one Finding per season; below MIN_GAMES it is info only."""
    def check(run: Run, scope: Scope) -> List[Finding]:
        extra, params = scope_where(scope)
        df = run(sql.format(extra=extra), params)
        if df is None or df.empty:
            return [Finding(check_id, severity, "all/none", 0, 0, {})]
        out = []
        for _, r in df.iterrows():
            vals = {c: (int(r[c]) if c == "n" else _num(r[c])) for c in df.columns if c != "season"}
            enough = vals["n"] >= MIN_GAMES
            out.append(Finding(check_id, severity if enough else "info", f"{r['season']}/regular",
                               judge(vals) if enough else 0, n_metrics, vals))
        return out
    check.__name__ = f"check_{check_id.lower()}"
    return check


def _judge_e3(v: dict) -> int:
    bad = 0
    bad += not (SPREAD_CORR[0] <= (v["corr_v"] if v["corr_v"] is not None else -1) <= SPREAD_CORR[1])
    bad += not (SPREAD_MAE[0] <= (v["mae"] if v["mae"] is not None else 0) <= SPREAD_MAE[1])
    bad += not (abs(v["bias"] if v["bias"] is not None else 99) <= SPREAD_BIAS)
    return bad


def _judge_e5(v: dict) -> int:
    bad = 0
    bad += not ((v["corr_total"] if v["corr_total"] is not None else -1) >= TOTAL_CORR_MIN)
    bad += not (abs(v["mean_err"] if v["mean_err"] is not None else 99) <= TOTAL_ERR_MAX)
    return bad


def _judge_e7(v: dict) -> int:
    return int(not ((v["corr_v"] if v["corr_v"] is not None else -1) > 0))


check_e3_spread_health = _per_season("E3", "error", _E3, _judge_e3, 3)
check_e5_total_health = _per_season("E5", "warn", _E5, _judge_e5, 2)
check_e7_line_flip = _per_season("E7", "error", _E7, _judge_e7, 1)
