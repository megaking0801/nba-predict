# Project Context: NBA Daily Pipeline

## Repo Structure
- .github/workflows/daily_nba.yml
- jobs/cache_nba.py
- jobs/sync_daily.py
- jobs/train_base_model.py
- jobs/train_calibrator.py
- jobs/auto_nba_predict.py
- jobs/settle_daily.py

## Purpose
Daily pipeline to cache data, build features, train/update model, produce predictions, and settle results.

## Data Sources
- Primary: cdn.nba.com staticData schedule + liveData boxscore
- Secondary (optional): ESPN scoreboard (only if needed)

## DB Tables
- public.nba_cache(cache_key PK, payload_json, updated_at_tw)
- (list other tables your pipeline writes/reads)

## Cache Keys
- cache_meta
- player_stats:{season}  (source: ??? schema: ???)
- team_log:{season}:{abbr} (schema: ???)

## Job Flow
1. cache_nba.py
   - Input: env NBA_SEASON, OVERRIDE_US_DATE, CACHE_PAST_DAYS, CACHE_FUTURE_DAYS, TEAM_LOG_LAST_N
   - Output: nba_cache keys above

2. sync_daily.py
   - Reads: nba_cache keys ...
   - Writes: ...

3. train_base_model.py
   - Reads: ...
   - Writes: ...

4. train_calibrator.py
   - Reads: ...
   - Writes: ...

5. auto_nba_predict.py
   - Reads: ...
   - Writes: ...

6. settle_daily.py
   - Reads: ...
   - Writes: ...

## Environment Variables
- DATABASE_URL or SUPABASE_*...
- NBA_SEASON=
- OVERRIDE_US_DATE=
- CACHE_PAST_DAYS=
- CACHE_FUTURE_DAYS=
- TEAM_LOG_LAST_N=
- ...

## Current Status
- Date:
- What works:
- Current issue:
- Next steps:
