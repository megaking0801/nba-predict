"""D (shot bucket) checks. SELECT only.

Shot buckets are NOT repaired to match the box score (see the design, 4.4): each
team-game is classified instead, and the share of each class is reported.
"""
from __future__ import annotations

from typing import Callable, List, Optional

import pandas as pd

from jobs.quality.core import Finding, Scope, grouped_check, scope_where

Run = Callable[[str, tuple], pd.DataFrame]
MIN_COMPARABLE = 200          # below this a share is only reported (info)


def shots_class(box_fga: Optional[int], bucket_fga: Optional[int]) -> str:
    """no_box (box FGA missing) | none (no bucket rows) | exact | minor (|diff| <= 2) | major."""
    if box_fga is None:
        return "no_box"
    if not bucket_fga:
        return "none"
    diff = abs(bucket_fga - box_fga)
    return "exact" if diff == 0 else ("minor" if diff <= 2 else "major")


_D1 = """/* qc:D1 */
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE coalesce(b.n, 0) = 0) AS n_bad
  FROM public.games_v2 g
  LEFT JOIN (SELECT game_id, count(*) AS n FROM public.team_shot_buckets GROUP BY game_id) b
         ON b.game_id = g.game_id
 WHERE g.status = 'final'
   AND EXISTS (SELECT 1 FROM public.player_game_stats p
                WHERE p.game_id = g.game_id AND p.min_played IS NOT NULL){extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

# Same classification as shots_class(), per team-game, counted in SQL.
_D2 = """/* qc:D2 */
WITH s AS (SELECT game_id, team_abbr, sum(fga) AS pfga FROM public.team_shot_buckets GROUP BY game_id, team_abbr)
SELECT g.season, g.season_type,
       count(*) FILTER (WHERE t.fga IS NOT NULL AND s.pfga = t.fga) AS n_exact,
       count(*) FILTER (WHERE t.fga IS NOT NULL AND s.pfga IS NOT NULL AND s.pfga <> t.fga
                           AND abs(s.pfga - t.fga) <= 2) AS n_minor,
       count(*) FILTER (WHERE t.fga IS NOT NULL AND s.pfga IS NOT NULL AND abs(s.pfga - t.fga) > 2) AS n_major,
       count(*) FILTER (WHERE t.fga IS NOT NULL AND coalesce(s.pfga, 0) = 0) AS n_none,
       count(*) FILTER (WHERE t.fga IS NULL) AS n_no_box
  FROM public.team_game_stats t
  JOIN public.games_v2 g ON g.game_id = t.game_id
  LEFT JOIN s ON s.game_id = t.game_id AND s.team_abbr = t.team_abbr
 WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_D3 = """/* qc:D3 */
WITH s AS (SELECT game_id, team_abbr, sum(fga) FILTER (WHERE pts_value = 3) AS p3a
             FROM public.team_shot_buckets GROUP BY game_id, team_abbr)
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE s.p3a IS DISTINCT FROM t.fg3a) AS n_bad
  FROM public.team_game_stats t
  JOIN public.games_v2 g ON g.game_id = t.game_id
  JOIN s ON s.game_id = t.game_id AND s.team_abbr = t.team_abbr
 WHERE g.status = 'final' AND t.fg3a IS NOT NULL{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_D4 = """/* qc:D4 */
SELECT g.season, g.season_type, coalesce(sum(b.fga), 0) AS n_total,
       coalesce(sum(b.fga) FILTER (WHERE b.dist_bucket = 'unknown'), 0) AS n_bad
  FROM public.team_shot_buckets b
  JOIN public.games_v2 g ON g.game_id = b.game_id
 WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_D5 = """/* qc:D5 */
SELECT g.season, g.season_type, coalesce(sum(b.fga), 0) AS n_total,
       coalesce(sum(b.fga) FILTER (
         WHERE (b.pts_value = 2 AND b.dist_bucket = '24+')
            OR (b.pts_value = 3 AND b.dist_bucket IN ('0-4', '4-8', '8-14'))), 0) AS n_bad
  FROM public.team_shot_buckets b
  JOIN public.games_v2 g ON g.game_id = b.game_id
 WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

check_d1_shots_present = grouped_check("D1", "error", _D1)
check_d5_shots_impossible_combo = grouped_check("D5", "warn", _D5)
check_d3_shots_3pa_match = grouped_check("D3", "info", _D3)
check_d4_shots_unknown_share = grouped_check("D4", "info", _D4)


def check_d2_shots_class_share(run: Run, scope: Scope) -> List[Finding]:
    """Per season and type: share of team-games in each class. The denominator counts
    exact/minor/major/none but never no_box (no box FGA to compare with)."""
    extra, params = scope_where(scope)
    df = run(_D2.format(extra=extra), params)
    if df is None or df.empty:
        return [Finding("D2", "warn", "all/none", 0, 0, {})]
    out = []
    for _, r in df.iterrows():
        exact, minor, major, none = (int(r[c] or 0) for c in ("n_exact", "n_minor", "n_major", "n_none"))
        n = exact + minor + major + none
        pct = (lambda x: round(100.0 * x / n, 2) if n else 0.0)
        out.append(Finding("D2", "warn" if n >= MIN_COMPARABLE else "info",
                           f"{r['season']}/{r['season_type']}", major + none, n,
                           {"exact_pct": pct(exact), "minor_pct": pct(minor), "major_pct": pct(major),
                            "none_pct": pct(none), "n_comparable": n, "n_no_box": int(r["n_no_box"] or 0)}))
    return out
