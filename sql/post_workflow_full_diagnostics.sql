-- =========================================================
-- NBA post-workflow full diagnostics (run after each workflow run)
-- Purpose: one-shot validation for sync/train/settle pipeline health
-- =========================================================

-- 0) server time / timezone sanity
SELECT
  NOW() AS server_now,
  NOW() AT TIME ZONE 'Asia/Taipei' AS server_now_tw;

-- 0.1) Preflight: ensure diagnostic columns exist (idempotent)
ALTER TABLE public.games ADD COLUMN IF NOT EXISTS pred_margin DOUBLE PRECISION;
ALTER TABLE public.games ADD COLUMN IF NOT EXISTS base_diff DOUBLE PRECISION;
ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_starters_out DOUBLE PRECISION;
ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_starters_out DOUBLE PRECISION;
ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_minutes_proj DOUBLE PRECISION;
ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_minutes_proj DOUBLE PRECISION;
ALTER TABLE public.games ADD COLUMN IF NOT EXISTS spread_move DOUBLE PRECISION;
ALTER TABLE public.games ADD COLUMN IF NOT EXISTS home_odds_move DOUBLE PRECISION;
ALTER TABLE public.games ADD COLUMN IF NOT EXISTS away_odds_move DOUBLE PRECISION;

-- 1) model registry latest status (base/calibrator)
WITH ranked AS (
  SELECT
    model_name,
    model_version,
    trained_rows,
    created_at_tw,
    metrics,
    ROW_NUMBER() OVER (PARTITION BY model_name ORDER BY created_at_tw DESC NULLS LAST) AS rn
  FROM public.model_registry
)
SELECT
  model_name,
  model_version,
  trained_rows,
  created_at_tw,
  metrics
FROM ranked
WHERE rn = 1
  AND (
    model_name IN ('margin_base_model', 'margin_calibrator', 'cover_prob_calibrator')
    OR model_name LIKE 'margin_calibrator_spread_%'
  )
ORDER BY model_name;

-- 2) games freshness and status distribution
SELECT
  MAX(to_date(game_date_us, 'MM/DD/YYYY')) AS latest_game_date,
  COUNT(*) AS total_rows,
  COUNT(*) FILTER (WHERE status='scheduled') AS scheduled_rows,
  COUNT(*) FILTER (WHERE status='in_progress') AS in_progress_rows,
  COUNT(*) FILTER (WHERE status='final') AS final_rows
FROM public.games;

-- 3) latest 14 dates row volume trend
SELECT
  game_date_us,
  COUNT(*) AS rows_per_day
FROM public.games
GROUP BY game_date_us
ORDER BY to_date(game_date_us, 'MM/DD/YYYY') DESC
LIMIT 14;

-- 4) game_id format + duplicate matchup/date sanity
SELECT
  COUNT(*) FILTER (WHERE game_id ~ '^[0-9]+$') AS numeric_game_id_rows,
  COUNT(*) FILTER (WHERE game_id !~ '^[0-9]+$') AS legacy_game_id_rows,
  COUNT(*) AS total_rows
FROM public.games;

SELECT
  season,
  game_date_us,
  away_abbr,
  home_abbr,
  COUNT(*) AS dup_rows
FROM public.games
GROUP BY season, game_date_us, away_abbr, home_abbr
HAVING COUNT(*) > 1
ORDER BY to_date(game_date_us, 'MM/DD/YYYY') DESC, dup_rows DESC
LIMIT 100;

-- 5) critical null diagnostics (features + outputs)
SELECT
  COUNT(*) FILTER (WHERE margin IS NULL) AS null_margin,
  COUNT(*) FILTER (WHERE cover IS NULL) AS null_cover,
  COUNT(*) FILTER (WHERE cover_prob IS NULL) AS null_cover_prob,
  COUNT(*) FILTER (WHERE base_diff IS NULL) AS null_base_diff,

  COUNT(*) FILTER (WHERE home_pts_sum IS NULL OR away_pts_sum IS NULL) AS null_pts_sum_pair,
  COUNT(*) FILTER (WHERE home_impact_mean IS NULL OR away_impact_mean IS NULL) AS null_impact_pair,
  COUNT(*) FILTER (WHERE home_b2b IS NULL OR away_b2b IS NULL) AS null_b2b_pair,
  COUNT(*) FILTER (WHERE home_recent_w IS NULL OR away_recent_w IS NULL) AS null_recent_w_pair,

  COUNT(*) FILTER (WHERE home_ts_pct IS NULL OR away_ts_pct IS NULL) AS null_ts_pct_pair,
  COUNT(*) FILTER (WHERE home_orb_rate IS NULL OR away_orb_rate IS NULL) AS null_orb_rate_pair,
  COUNT(*) FILTER (WHERE home_usage_proxy IS NULL OR away_usage_proxy IS NULL) AS null_usage_proxy_pair,
  COUNT(*) FILTER (WHERE home_onoff_proxy IS NULL OR away_onoff_proxy IS NULL) AS null_onoff_proxy_pair,

  COUNT(*) FILTER (WHERE home_starters_out IS NULL OR away_starters_out IS NULL) AS null_starters_out_pair,
  COUNT(*) FILTER (WHERE home_minutes_proj IS NULL OR away_minutes_proj IS NULL) AS null_minutes_proj_pair,
  COUNT(*) FILTER (WHERE spread_move IS NULL OR home_odds_move IS NULL OR away_odds_move IS NULL) AS null_market_move
FROM public.games;

-- 6) candidate training row availability check
SELECT
  COUNT(*) AS train_rows_margin,
  COUNT(*) FILTER (WHERE cover IN (0,1)) AS train_rows_cover01,
  COUNT(*) FILTER (
    WHERE margin IS NOT NULL
      AND home_pts_sum IS NOT NULL AND away_pts_sum IS NOT NULL
      AND home_impact_mean IS NOT NULL AND away_impact_mean IS NOT NULL
      AND home_b2b IS NOT NULL AND away_b2b IS NOT NULL
      AND home_recent_w IS NOT NULL AND away_recent_w IS NOT NULL
      AND home_ts_pct IS NOT NULL AND away_ts_pct IS NOT NULL
      AND home_orb_rate IS NOT NULL AND away_orb_rate IS NOT NULL
      AND home_usage_proxy IS NOT NULL AND away_usage_proxy IS NOT NULL
      AND home_onoff_proxy IS NOT NULL AND away_onoff_proxy IS NOT NULL
      AND home_starters_out IS NOT NULL AND away_starters_out IS NOT NULL
      AND home_minutes_proj IS NOT NULL AND away_minutes_proj IS NOT NULL
      AND home_spread IS NOT NULL AND home_odds IS NOT NULL AND away_odds IS NOT NULL
      AND spread_move IS NOT NULL AND home_odds_move IS NOT NULL AND away_odds_move IS NOT NULL
  ) AS train_rows_full_feature
FROM public.games;

-- 7) last-14-day scoring quality (MAE/Brier/accuracy)
WITH base AS (
  SELECT
    game_date_us,
    cover,
    cover_prob,
    pred_margin,
    base_diff,
    margin,
    home_score,
    away_score,
    (home_score - away_score) AS actual_margin
  FROM public.games
  WHERE home_score IS NOT NULL
    AND away_score IS NOT NULL
),
scored AS (
  SELECT
    *,
    CASE WHEN cover IN (0,1) AND cover_prob IS NOT NULL THEN ABS(cover_prob - cover::float) END AS abs_prob_err,
    CASE WHEN cover IN (0,1) AND cover_prob IS NOT NULL THEN POWER((cover_prob - cover::float), 2) END AS brier_item,
    CASE WHEN COALESCE(pred_margin, base_diff) IS NOT NULL THEN ABS(COALESCE(pred_margin, base_diff) - actual_margin) END AS margin_abs_err,
    CASE
      WHEN cover IN (0,1) AND cover_prob IS NOT NULL THEN
        CASE WHEN (cover_prob >= 0.5 AND cover = 1) OR (cover_prob < 0.5 AND cover = 0) THEN 1 ELSE 0 END
      ELSE NULL
    END AS cover_direction_hit
  FROM base
)
SELECT
  game_date_us,
  COUNT(*) AS settled_games,
  ROUND(AVG(abs_prob_err)::numeric, 6) AS mae_prob,
  ROUND(AVG(brier_item)::numeric, 6) AS brier_score,
  ROUND(AVG(margin_abs_err)::numeric, 4) AS mae_margin,
  ROUND(AVG(cover_direction_hit)::numeric, 4) AS direction_acc
FROM scored
GROUP BY game_date_us
ORDER BY to_date(game_date_us, 'MM/DD/YYYY') DESC
LIMIT 14;

-- 8) slice evaluation (home/away b2b, spread buckets) for last 30 days
WITH base AS (
  SELECT
    to_date(game_date_us, 'MM/DD/YYYY') AS game_date,
    home_b2b,
    away_b2b,
    ABS(COALESCE(home_spread, 0)) AS abs_spread,
    cover,
    cover_prob,
    pred_margin,
    base_diff,
    margin,
    (home_score - away_score) AS actual_margin
  FROM public.games
  WHERE to_date(game_date_us, 'MM/DD/YYYY') >= CURRENT_DATE - INTERVAL '30 days'
    AND home_score IS NOT NULL
    AND away_score IS NOT NULL
),
expanded AS (
  SELECT 'home_b2b=1' AS slice_name, * FROM base WHERE COALESCE(home_b2b,0)=1
  UNION ALL
  SELECT 'away_b2b=1' AS slice_name, * FROM base WHERE COALESCE(away_b2b,0)=1
  UNION ALL
  SELECT 'spread_0_3' AS slice_name, * FROM base WHERE abs_spread < 3
  UNION ALL
  SELECT 'spread_3_6' AS slice_name, * FROM base WHERE abs_spread >= 3 AND abs_spread < 6
  UNION ALL
  SELECT 'spread_6p' AS slice_name, * FROM base WHERE abs_spread >= 6
)
SELECT
  slice_name,
  COUNT(*) AS rows,
  ROUND(AVG(CASE WHEN COALESCE(pred_margin, base_diff) IS NOT NULL THEN ABS(COALESCE(pred_margin, base_diff) - actual_margin) END)::numeric, 4) AS mae_margin,
  ROUND(AVG(CASE WHEN cover IN (0,1) AND cover_prob IS NOT NULL THEN POWER((cover_prob - cover::float),2) END)::numeric, 6) AS brier_cover,
  ROUND(AVG(CASE WHEN cover IN (0,1) AND cover_prob IS NOT NULL
                 THEN CASE WHEN (cover_prob>=0.5 AND cover=1) OR (cover_prob<0.5 AND cover=0) THEN 1 ELSE 0 END
                 END)::numeric, 4) AS acc_cover
FROM expanded
GROUP BY slice_name
ORDER BY rows DESC, slice_name;

-- 9) optional safe backfill for b2b NULLs (uncomment to execute)
-- UPDATE public.games
-- SET home_b2b = COALESCE(home_b2b, 0),
--     away_b2b = COALESCE(away_b2b, 0)
-- WHERE home_b2b IS NULL OR away_b2b IS NULL;
