# Project Context: NBA Daily Pipeline

## Repo Structure
- `.github/workflows/daily_nba.yml`
- `jobs/cache_nba.py`
- `jobs/sync_daily.py`
- `jobs/train_base_model.py`
- `jobs/train_calibrator.py`
- `jobs/settle_daily.py`
- `auto_nba_predict.py` (Streamlit app，手動操作/檢視用)

## Purpose
此專案的目標是每天自動完成以下流程：
1. 快取必要資料（球員季統計、球隊比賽 log）到 DB。
2. 同步當日/回補區間賽程與賠率，產生特徵與預測欄位。
3. 結算已完賽事（margin / cover）。
4. 以歷史資料訓練/更新 base model 與 calibrator。

重點是**每日可持續運行**，並且在 `stats.nba.com` 不穩時，仍可透過既有 cache + ESPN/Odds 流程維持 `sync/settle/train`。

## Data Sources
### Primary (production path)
- ESPN scoreboard API：
  - URL: `https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard`
  - 用途：賽程、對戰組合、即時/完賽比分、完賽判定。
- The Odds API：
  - 用途：盤口、賠率（`home_spread`, `home_odds`, `away_odds`）。

### Auxiliary (cache refresh only)
- `stats.nba.com`（透過 `nba_api` endpoint）
  - 用途：在 `cache_nba.py` 產生 `player_stats:{season}` 與 `team_log:{season}:{abbr}`。
  - 設計重點：`cache_put_if_nonempty` 避免空資料覆蓋既有 cache，並有 circuit breaker 防止持續失敗時過度請求。

## DB Tables (actual read/write)

### `public.nba_cache`
- Key-value cache table。
- Columns: `cache_key (PK)`, `payload_json`, `updated_at_tw`。
- Writers:
  - `jobs/cache_nba.py`
- Readers:
  - `jobs/sync_daily.py`

### `public.games`
- 每場比賽主表（賽程、盤口、特徵、預測、結果）。
- 主要欄位群：
  - Identity: `game_id`, `game_date_us`, `season`, `away_abbr`, `home_abbr`
  - Market: `home_spread`, `home_odds`, `away_odds`, `line_source`
  - Features: `home_pts_sum`, `away_pts_sum`, `home_impact_mean`, `away_impact_mean`, `home_b2b`, `away_b2b`, `home_recent_w`, `away_recent_w`
  - Prediction: `base_diff`, `f_edge`, `cover_prob`, `implied_prob`, `edge_value`, `ev`, `pick_team`, `odds_used`
  - Settlement: `status`, `away_score`, `home_score`, `margin`, `cover`, `settled_at_tw`
  - Audit: `created_at_tw`, `updated_at_tw`, `game_date_tw`
- Writers:
  - `jobs/sync_daily.py`（upsert game rows）
  - `jobs/settle_daily.py`（update finals/cover）
- Readers:
  - `jobs/train_base_model.py`
  - `jobs/train_calibrator.py`
  - `auto_nba_predict.py`

### `public.model_registry`
- 模型序列化儲存。
- Columns: `model_name (PK)`, `model_version`, `payload_base64`, `trained_rows`, `metrics`, `created_at_tw`。
- Writers:
  - `jobs/train_base_model.py`（`margin_base_model`）
  - `jobs/train_calibrator.py`（`cover_prob_calibrator`）
- Readers:
  - `jobs/sync_daily.py`（讀 calibrator）
  - `auto_nba_predict.py`

## Cache Keys
- `cache_meta`
  - schema:
    - `season`
    - `anchor_us`
    - `window.past_days`
    - `window.future_days`
    - `teams_from_espn`
    - `teams_cached`
    - `stats_circuit_breaker` (`fail_threshold`, `consecutive_failures`, `opened`)
    - `updated_at_tw`
- `player_stats:{season}`
  - source: `league dash player stats`（stats endpoint via cache job）
  - schema: `{ season, rows:[{ PLAYER_NAME, GP, MIN, PTS, REB, AST, STL, BLK, TOV, ...}] }`
- `team_log:{season}:{abbr}`
  - source: `team game log`（stats endpoint via cache job）
  - schema: `{ season, abbr, team_id, rows:[{ GAME_DATE, MATCHUP, W_L, PTS, ...}] }`

## Job Flow
1. `cache_nba.py`
   - Inputs:
     - `NBA_SEASON`, `OVERRIDE_US_DATE`, `CACHE_PAST_DAYS`, `CACHE_FUTURE_DAYS`
     - DB env (`DATABASE_URL` or `SUPABASE_*`)
   - External dependencies:
     - ESPN（決定需要更新哪些隊伍）
     - stats endpoint（抓 player/team logs）
   - Writes:
     - `public.nba_cache` (`player_stats:*`, `team_log:*`, `cache_meta`)
   - Failure behavior:
     - stats 抓不到且 `rows=0` 時，不覆蓋舊 cache。
     - 連續失敗達門檻時開啟 circuit breaker，停止後續 stats 抓取。

2. `sync_daily.py`
   - Inputs:
     - `NBA_SEASON`, `OVERRIDE_US_DATE`, `BACKFILL_PAST_DAYS`, `BACKFILL_FUTURE_DAYS`, `FAST_MODE`
     - `ODDS_API_KEY`（可選；無時使用 fallback lines）
     - DB env
   - Reads:
     - `public.nba_cache` (`player_stats:*`, `team_log:*`)
     - `public.model_registry` (`cover_prob_calibrator`)
   - External dependencies:
     - ESPN（賽程/比分）
     - Odds API（盤口）
     - Rotowire（傷病，失敗可忽略）
   - Writes:
     - upsert `public.games`
   - Failure behavior:
     - ESPN 該日抓取失敗：跳過該日（不中斷整批日期）。
     - Odds 缺資料：未來賽事使用 fallback line；歷史賽事保留 NULL 以免覆蓋。
     - cache 缺資料：特徵可能為 NULL，但流程可繼續寫入 games。

3. `train_base_model.py`
   - Inputs: `BASE_MIN_ROWS`（預設 300）+ DB env
   - Reads: `public.games`（已結算且特徵完整資料）
   - Writes: `public.model_registry` (`margin_base_model`)
   - Failure behavior: 樣本不足時 skip，不覆蓋既有模型。

4. `train_calibrator.py`
   - Inputs: `CAL_MIN_ROWS`（預設 200）+ DB env
   - Reads: `public.games`（`f_edge`, `cover`）
   - Writes: `public.model_registry` (`cover_prob_calibrator`)
   - Failure behavior: 樣本不足時 skip，不覆蓋既有 calibrator。

5. `settle_daily.py`
   - Inputs: `OVERRIDE_US_DATE`, `SETTLE_LOOKBACK_DAYS` + DB env
   - Reads:
     - ESPN finals
     - `public.games`（讀既有 spread 來計算 cover）
   - Writes:
     - update `public.games` (`status`, `away_score`, `home_score`, `margin`, `cover`, `settled_at_tw`, `updated_at_tw`)

## Environment Variables
### DB
- `DATABASE_URL` 或以下 supabase 參數：
  - `SUPABASE_HOST`, `SUPABASE_DB`, `SUPABASE_USER`, `SUPABASE_PASSWORD`, `SUPABASE_PORT`

### Cache job
- `NBA_SEASON`
- `OVERRIDE_US_DATE` (`MM/DD/YYYY`)
- `CACHE_PAST_DAYS`
- `CACHE_FUTURE_DAYS`
- `NBA_STATS_CB_THRESHOLD`
- `NBA_TEAM_SLEEP`

### Sync job
- `NBA_SEASON`
- `OVERRIDE_US_DATE`
- `BACKFILL_PAST_DAYS`
- `BACKFILL_FUTURE_DAYS`
- `FAST_MODE` (`0/1`)
- `ODDS_API_KEY`
- `PROB_SCALE`, `PROB_FLOOR`, `PROB_CEIL`

### Train jobs
- `BASE_MIN_ROWS`
- `CAL_MIN_ROWS`

### Settle job
- `OVERRIDE_US_DATE`
- `SETTLE_LOOKBACK_DAYS`

## Operational Runbook (for stability)

### Standard daily order
1. `cache_nba.py`
2. `sync_daily.py`
3. `settle_daily.py`
4. `train_base_model.py`
5. `train_calibrator.py`

### When stats endpoint unstable (目標：不中斷 daily pipeline)
- 允許 `cache_nba.py` 部分失敗；不要清掉 `nba_cache` 舊資料。
- 仍執行 `sync_daily.py` + `settle_daily.py`（依賴 ESPN/Odds + 舊 cache）。
- 訓練 job 仍可跑，但若樣本不足會自動 skip。

### Minimum health checks
- `cache_nba.py` log 出現 `cache_meta` 寫入成功。
- `sync_daily.py` 結尾 `sync complete rows=...`。
- `settle_daily.py` 有 `updated rows=...`。
- `model_registry` 兩個模型 key 可查到最近版本（若樣本不足允許不更新）。

## Current Status
- Date: 2026-02-24
- What works:
  - 排程 workflow 已串起 cache/sync/settle/train 全流程。
  - `sync/settle` 的主要資料源為 ESPN + Odds + DB cache，可在 stats endpoint 不穩時維持核心流程。
- Current issue:
  - cache refresh 仍需 stats endpoint 才能更新最新 player/team cache。
- Next steps (僅記錄，不在本次 Step 1 變更範圍):
  - 以非 stats 來源補齊長期可替代的 player/team 特徵來源，逐步降低 cache refresh 對 stats endpoint 的依賴。
