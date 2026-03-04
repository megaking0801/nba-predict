-- NBA pipeline one-click diagnosis + safe backfill helpers
-- Usage: run whole file in Supabase SQL editor (top to bottom).

-- =========================================================
-- 0) Quick environment sanity
-- =========================================================
SELECT NOW() AS server_time_utc;

-- 0.1) Preflight: keep diagnostics SQL backward-compatible with older schema
ALTER TABLE public.games ADD COLUMN IF NOT EXISTS pred_margin DOUBLE PRECISION;

-- =========================================================
-- 1) Data freshness and row counts
-- =========================================================
SELECT
  MAX(game_date_us) AS latest_game_date_us,
  COUNT(*)          AS total_games
FROM public.games;

SELECT
  MAX(game_date_us) AS latest_player_boxscore_date_us,
  COUNT(*)          AS total_player_rows
FROM public.game_player_stats;

-- =========================================================
-- 2) NULL diagnostics (features and settle-related columns)
-- =========================================================
SELECT
  COUNT(*) FILTER (WHERE home_b2b IS NULL)              AS null_home_b2b,
  COUNT(*) FILTER (WHERE away_b2b IS NULL)              AS null_away_b2b,
  COUNT(*) FILTER (WHERE home_ts_pct IS NULL)           AS null_home_ts_pct,
  COUNT(*) FILTER (WHERE away_ts_pct IS NULL)           AS null_away_ts_pct,
  COUNT(*) FILTER (WHERE home_orb_rate IS NULL)         AS null_home_orb_rate,
  COUNT(*) FILTER (WHERE away_orb_rate IS NULL)         AS null_away_orb_rate,
  COUNT(*) FILTER (WHERE home_usage_proxy IS NULL)      AS null_home_usage_proxy,
  COUNT(*) FILTER (WHERE away_usage_proxy IS NULL)      AS null_away_usage_proxy,
  COUNT(*) FILTER (WHERE home_onoff_proxy IS NULL)      AS null_home_onoff_proxy,
  COUNT(*) FILTER (WHERE away_onoff_proxy IS NULL)      AS null_away_onoff_proxy,
  COUNT(*) FILTER (WHERE cover IS NULL)                 AS null_cover,
  COUNT(*) FILTER (WHERE home_score IS NULL)            AS null_home_score,
  COUNT(*) FILTER (WHERE away_score IS NULL)            AS null_away_score
FROM public.games;

-- =========================================================
-- 3) game_id shape check (numeric = ESPN event id)
-- =========================================================
SELECT
  COUNT(*) FILTER (WHERE game_id ~ '^[0-9]+$') AS numeric_game_id_rows,
  COUNT(*) FILTER (WHERE game_id !~ '^[0-9]+$') AS legacy_game_id_rows,
  COUNT(*)                                      AS total_rows
FROM public.games;

-- potential duplicated matchup/date due to mixed game_id styles
SELECT
  season,
  game_date_us,
  away_abbr,
  home_abbr,
  COUNT(*) AS dup_rows
FROM public.games
GROUP BY season, game_date_us, away_abbr, home_abbr
HAVING COUNT(*) > 1
ORDER BY game_date_us DESC, dup_rows DESC
LIMIT 100;

-- =========================================================
-- 4) Safe backfills (idempotent)
-- =========================================================
-- 4.1 Fill b2b nulls
UPDATE public.games
SET home_b2b = COALESCE(home_b2b, 0),
    away_b2b = COALESCE(away_b2b, 0)
WHERE home_b2b IS NULL
   OR away_b2b IS NULL;

-- 4.2 Normalize impossible numeric NaNs represented as NULLs for boxscore features
UPDATE public.games
SET
  home_ts_pct       = COALESCE(home_ts_pct, 0),
  away_ts_pct       = COALESCE(away_ts_pct, 0),
  home_orb_rate     = COALESCE(home_orb_rate, 0),
  away_orb_rate     = COALESCE(away_orb_rate, 0),
  home_usage_proxy  = COALESCE(home_usage_proxy, 0),
  away_usage_proxy  = COALESCE(away_usage_proxy, 0),
  home_onoff_proxy  = COALESCE(home_onoff_proxy, 0),
  away_onoff_proxy  = COALESCE(away_onoff_proxy, 0)
WHERE home_ts_pct IS NULL
   OR away_ts_pct IS NULL
   OR home_orb_rate IS NULL
   OR away_orb_rate IS NULL
   OR home_usage_proxy IS NULL
   OR away_usage_proxy IS NULL
   OR home_onoff_proxy IS NULL
   OR away_onoff_proxy IS NULL;

-- =========================================================
-- 5) Yesterday accuracy dashboard (fixed ROUND cast issue)
-- =========================================================
WITH base AS (
  SELECT
    game_date_us,
    game_id,
    away_abbr,
    home_abbr,
    cover,
    cover_prob,
    pred_margin,
    home_score,
    away_score,
    (home_score - away_score) AS actual_margin
  FROM public.games
  WHERE cover IN (0,1,2)
    AND home_score IS NOT NULL
    AND away_score IS NOT NULL
),
scored AS (
  SELECT
    *,
    CASE WHEN cover IN (0,1) THEN ABS(cover_prob - cover::float) END AS abs_prob_err,
    CASE WHEN cover IN (0,1) THEN POWER((cover_prob - cover::float), 2) END AS brier_item,
    CASE WHEN pred_margin IS NOT NULL THEN ABS(pred_margin - actual_margin) END AS margin_abs_err
  FROM base
)
SELECT
  game_date_us,
  COUNT(*) AS settled_games,
  ROUND(AVG(abs_prob_err)::numeric, 6)   AS mae_prob,
  ROUND(AVG(brier_item)::numeric, 6)     AS brier_score,
  ROUND(AVG(margin_abs_err)::numeric, 4) AS mae_margin,
  ROUND(AVG(CASE WHEN cover IN (0,1) AND cover_prob >= 0.5 AND cover = 1 THEN 1
                 WHEN cover IN (0,1) AND cover_prob < 0.5  AND cover = 0 THEN 1
                 WHEN cover IN (0,1) THEN 0 END)::numeric, 4) AS direction_acc
FROM scored
GROUP BY game_date_us
ORDER BY to_date(game_date_us, 'MM/DD/YYYY') DESC
LIMIT 14;
