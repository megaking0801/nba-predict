"""B (box score) checks. SELECT only; counts are in team-games unless noted."""
from __future__ import annotations

import math

from jobs.quality.core import grouped_check

TEAM_STAT_COLS = ("fga", "fgm", "fg3a", "fta", "tov", "oreb", "dreb")


def minutes_ok(total_minutes: float) -> bool:
    """A team plays 240 + 25*k minutes (k extra overtimes); allow +/-3 for rounding."""
    k = max(0, math.floor((total_minutes - 240) / 25.0 + 0.5))
    return abs(total_minutes - (240 + 25 * k)) <= 3


_COMPLETE = " AND ".join(f"t.{c} IS NOT NULL" for c in TEAM_STAT_COLS)

_B1 = f"""/* qc:B1 */
SELECT g.season, g.season_type,
       2 * count(DISTINCT g.game_id) AS n_total,
       2 * count(DISTINCT g.game_id)
         - count(t.team_abbr) FILTER (WHERE {_COMPLETE}) AS n_bad
  FROM public.games_v2 g
  LEFT JOIN public.team_game_stats t ON t.game_id = g.game_id
 WHERE g.status = 'final'{{extra}}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_B2 = """/* qc:B2 */
WITH p AS (SELECT game_id, team_abbr, count(min_played) AS n_min
             FROM public.player_game_stats GROUP BY game_id, team_abbr)
SELECT g.season, g.season_type,
       2 * count(DISTINCT g.game_id) AS n_total,
       2 * count(DISTINCT g.game_id) - count(p.team_abbr) FILTER (WHERE p.n_min > 0) AS n_bad
  FROM public.games_v2 g
  LEFT JOIN p ON p.game_id = g.game_id
 WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_B3 = """/* qc:B3 */
WITH p AS (SELECT game_id, team_abbr, count(min_played) AS n_min, sum(pts) AS pp
             FROM public.player_game_stats GROUP BY game_id, team_abbr)
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE p.pp IS DISTINCT FROM t.pts) AS n_bad,
       count(*) FILTER (WHERE abs(p.pp - t.pts) > 3) AS off_gt_3
  FROM public.team_game_stats t
  JOIN public.games_v2 g ON g.game_id = t.game_id
  JOIN p ON p.game_id = t.game_id AND p.team_abbr = t.team_abbr
 WHERE g.status = 'final' AND p.n_min > 0{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_B4 = """/* qc:B4 */
WITH p AS (SELECT game_id, team_abbr, count(min_played) AS n_min, sum(min_played) AS pm
             FROM public.player_game_stats GROUP BY game_id, team_abbr)
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE abs(p.pm - (240 + 25 * greatest(0, round((p.pm - 240) / 25.0)))) > 3) AS n_bad
  FROM public.games_v2 g
  JOIN p ON p.game_id = g.game_id
 WHERE g.status = 'final' AND p.n_min > 0{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_B5 = """/* qc:B5 */
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE p.fgm > p.fga OR p.ftm > p.fta OR p.fg3m > p.fg3a
                           OR p.pts < 0 OR p.min_played > 60) AS n_bad
  FROM public.player_game_stats p
  JOIN public.games_v2 g ON g.game_id = p.game_id
 WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

_B6 = """/* qc:B6 */
SELECT g.season, g.season_type, count(*) AS n_total,
       count(*) FILTER (WHERE t.pts <> CASE WHEN t.is_home THEN g.home_score ELSE g.away_score END) AS n_bad
  FROM public.team_game_stats t
  JOIN public.games_v2 g ON g.game_id = t.game_id
 WHERE g.status = 'final'{extra}
 GROUP BY g.season, g.season_type ORDER BY g.season, g.season_type
"""

check_b1_team_box_complete = grouped_check("B1", "warn", _B1)
check_b2_player_box_not_placeholder = grouped_check("B2", "warn", _B2)
check_b3_player_points_match_team = grouped_check("B3", "warn", _B3, ("off_gt_3",))
check_b4_team_minutes_240 = grouped_check("B4", "warn", _B4)
check_b5_impossible_lines = grouped_check("B5", "error", _B5)
check_b6_team_points_equal_score = grouped_check("B6", "error", _B6)
