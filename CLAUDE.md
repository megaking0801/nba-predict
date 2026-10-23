# NBA 勝負預測

球迷向的 NBA 預測網站（Streamlit + Supabase Postgres）。模型預測主客場分差，再換算成勝率、讓分過盤機率與大小分估計。
定位是數據預測與娛樂，**不是投注建議**。高命中率不等於能獲利，介面與文案要維持這個誠實立場。

<!-- 維護筆記：本檔目標 200 行內，只放「讀程式碼猜不到」的內容。改完用 /doctor prompt-audit 檢查矛盾與過期內容。 -->

## 指令
```bash
python -m pytest tests -q            # 測試，約 2 秒，需在 repo 根目錄執行
streamlit run auto_nba_predict.py    # 啟動網站，需 DATABASE_URL 或 SUPABASE_* 環境變數，或 .streamlit/secrets.toml
python -m jobs.config                # 印出全部參數與 CONFIG_HASH
python -m jobs.evaluate              # walk-forward 評估 + ATS 回測（只讀，但需要 DB 連線）
python -m jobs.diagnose              # DB 狀態體檢（只讀，需要 DB 連線）
python -m webui.build_site --out site  # 從 DB 產生靜態網站到 site/（只讀連線，需要 DB 連線；site/ 不提交）
```
會寫入 DB 的 job（`schema`、`ingest_*`、`backfill`、`settle`、`snapshot_*`、`predict_daily`、`predict_history`、`train`、`calibrate`、`run_daily`）
平常由 GitHub Actions `daily_nba.yml` 排程執行，不在本機跑。

## 架構
- `auto_nba_predict.py`：Streamlit 前端外殼，只讀 DB（`v_app_board` 等），不在前端跑模型。渲染與 SQL 已抽到 `webui/`。
- `webui/`：`render.py`（純 HTML 渲染，不 import streamlit、不查 DB）、`queries.py`（只含 SELECT）、`build_site.py`（產生靜態站）。
  靜態站由 `.github/workflows/site.yml` 部署到 GitHub Pages（`daily_nba` 完成後、手動、或 `webui/**` 等路徑變動時觸發）。
- `jobs/`：v2 流程，資料只走 ESPN。`ingest_espn.py`（比賽、box score、盤口；scoreboard 抓失敗過半會 raise）、`snapshot_injuries.py`（傷兵）、`snapshot_rosters.py`（名單；隊伍 id 每次從 ESPN `/teams` 取）
  → `features.py`（`FeatureBuilder`）→ `train.py` / `calibrate.py` / `evaluate.py` → `predict_daily.py`（用 `picks.py` 決定出手）→ `settle.py`。
- 所有參數集中在 `jobs/config.py`；每個模型版本與預測都記錄 `CONFIG_HASH`。
- DDL 在 `jobs/schema.py`。表：`games_v2`（含 `neutral_site`）、`team_game_stats`、`player_game_stats`（只含有上場的球員，沒有 DNP 列）、`market_lines`（含 `price_point`／`total_price_point`，標示讓分與總分各取自 close／current／open）、`injury_snapshots`、`predictions`、`model_registry_v2`。
  P2 新表：`player_game_status`（box score 列出的每位球員：先發、DNP 與原因、驅逐）、`game_officials`、`team_shot_buckets`（每隊每場的投籃距離桶，罰球不計；P4 自行重新分組）。
  `neutral_site` 來自 scoreboard，**抓不到 2020 泡泡賽**（實測為 False），P4 要用日期區間標記泡泡。
  `ingest_espn` 的 `daily()`／`backfill()` 會先 `ensure_schema`，因為 `run_daily` 在 ESPN 抓取前不會呼叫它。
  view：`v_latest_lines`、`v_closing_lines`、`v_app_board`。
- 排程：10:30 UTC（台北 18:30）`run_daily` 全流程，是當天的正式預測；22:30、00:30、02:00 UTC 跑 `run_daily --close`（只抓開賽前 150 分鐘內比賽的盤口與傷兵，**不預測**）；週一 12:00 UTC 重新訓練與 keepalive。
- 傷兵與名單歷史：`injury_reports`（每次抓取 append，含 ESPN 的 `reported_at`）、`roster_snapshots`（每隊每個 ET 日一份）；舊表 `injury_snapshots` 照寫，`predict_daily` 讀它。
- workflow 設 `PAPER_MODE=1`：模型證明贏得了市場（P5）之前，預測一律記為 paper。
- `track_record()` 每場只算一次（`DISTINCT ON`，優先取開賽前最後一筆；是偏好不是過濾，回填的歷史預測才不會歸零）。
- 模型通過 `evaluate.py` 的 gates 才會啟用；訓練列數低於 `MIN_TRAIN_ROWS` 時不訓練。

## 不能違反的規則
**特徵與模型**
- 特徵一律「先 `emit`、再 `update`」（`jobs/features.py`）。新特徵必須通過 `tests/test_features_point_in_time.py` 的無洩漏 golden test。
- 隊名只能走 `jobs/teams.py` 的 `normalize_espn_abbr` / `require_abbr`。未知縮寫要報錯，不准默默補零（舊系統曾讓 6 隊特徵歸零）。
- 校準與驗證只能用時間順序切分，不准用隨機 KFold。
- 參數只改 `jobs/config.py`；改特徵清單要升 `FEATURE_SET` / `FEATURE_SET_TOTAL`。
- 不為了通過 gate 而放寬門檻。要放寬，先報告原因與 `evaluate` 改前改後的數字。
- 不要用最終模型回填歷史預測再當戰績（in-sample）。可信的數字是 walk-forward，`predict_history.py` 是正確做法。
- 大小分方向命中率約 48%（walk-forward；實際數字看 `total_model` 的 metrics，可能已變）。App 以「模型估總分」為主並附警語，不要做成有信心的大小方向下注。
- `train_cal`（舊校準）是刻意從 workflow 移除的，不要加回去。

**資料與 DB**
- 時間：DB 存 UTC；比賽日用 ET；台北時間只出現在 App 顯示層。
- schema 變更只做可重複執行、只新增的 migration（`ADD COLUMN IF NOT EXISTS`）。`v_app_board` 的新欄位只能加在最後，`CREATE OR REPLACE VIEW` 不能重排欄位。
- `market_lines`、`predictions` 設計上是 append-only。除了 `predict_history.py` 與 `ingest_espn --odds --force-rebuild` 這兩個既有的重建流程，不要刪改歷史列。`--odds` 預設拒絕執行，加 `--force-rebuild` 也只刪回填列（`captured_at` 等於開賽時間），不會動即時盤口快照；一般補盤口用 `backfill_odds_missing`（只補缺的，不刪）。
- `webui/queries.py` 只准 SELECT；`webui/render.py` 不 import streamlit，DB 來的文字一律經 `esc()`。`build_site` 連線必須 `readonly`。
- Streamlit 頁面輸出由 `tests/test_streamlit_golden.py` 的整頁 golden 鎖住；改渲染時 golden 不可因搬動而變，要改就是刻意的行為變更並說明。
- 新的資料來源用 ESPN。stats.nba.com 會擋 GitHub runner 與非美國 IP。

**安全與營運**
- 不讀、不印、不 commit `.streamlit/secrets.toml`（已在 `.gitignore` 與 `settings.json` 的 deny）。
- 未經同意，不跑會寫入 DB 的 job、不手動觸發 workflow_dispatch、不 push、不 force push、不 merge。所有環境共用同一個正式 Supabase。
- 新依賴必須能在 Streamlit Cloud 建置（`lxml` 曾因此被移除，見 commit 64231f3）。`requirements.txt` 版本維持固定。

## 工作流程
- 動到特徵、模型、schema、gates、picks 之前先進 plan mode。只改一兩行的小修不用。
- 修根因，不要壓掉錯誤訊息。
- 對話被壓縮時保留：已修改的檔案清單、測試指令、尚未解決的問題。
- 回覆用繁體中文；程式碼、識別字、commit 訊息用英文。

## 完成前驗證
先驗證再回報。貼出實際執行的指令與輸出，不要只說「應該可以」。

| 改了什麼 | 要做什麼 |
| :- | :- |
| 任何 `.py` | `python -m pytest tests -q` |
| 特徵、模型、gates、picks、`config.py` | 另外跑 `python -m jobs.evaluate`，貼出改前改後的 MAE / Brier |
| 前端 | 啟動 Streamlit 確認能載入；沒有 DB 連線就明說「未驗證」 |
| schema / SQL | 說明是否可重複執行；沒有真的跑過就明說「未驗證」 |

- 引用的數字、行號、檔名，回頭對照原始碼再說。
- 現有測試只涵蓋機率數學、特徵無洩漏、回測記帳。`calibrate`、`ingest_*`、`predict_daily`、前端沒有測試，動到要自己多一道檢查。
- 測試失敗就照實貼出輸出，不要說成功。沒驗證的部分直接寫「未驗證」。
- `.claude/hooks/pytest_gate.py` 是 Stop hook：有 `.py` 變更時自動跑 pytest，失敗會擋一次結束。

## 子代理與模型分工
- 預設在主對話做。只有符合下列情況才開子代理：要讀很多檔案的唯讀調查、會產生大量輸出（log、測試）、可平行的獨立工作、需要全新視角的審查。
  小修改、需要來回討論的事不要開，子代理冷啟動而且吃同一份用量。
- 專案子代理在 `.claude/agents/`：
  - `explorer`（haiku）：找定義與用法、摘要 log 與 Actions 輸出。唯讀。
  - `builder`（sonnet）：一般修改，如 UI、單一模組、補測試、文件。
  - `reviewer`（opus）：審查 diff。動到模型、統計、洩漏、校準、gates，或跨檔重構、原因不明的除錯，都交給它。唯讀。
- 開子代理時，在 prompt 裡重述關鍵規則；內建的 Explore 與 Plan 不會載入本檔。

## Git
- 未經同意不 push。commit 訊息用英文，簡短說明做了什麼與原因。
- 不要改動已被追蹤的 `tests/__pycache__/*.pyc`（待清理）。

## 已知待釐清（不要擅自改）
- `daily_nba` 因 60 天無活動被停用（`disabled_inactivity`），程式已改走 ESPN，但**尚未啟用**；啟用前靜態站內容停在最後一次資料，頁面 36 小時後顯示「資料可能已過期」。新表（`injury_reports`、`roster_snapshots`）與 2026-27 的比賽都還沒寫進正式 DB。
- `daily_nba.yml` 的 `keepalive` job 每週用 API 重新啟用 workflow，但「重新啟用能重置 60 天計時」**官方文件沒有說明（未驗證）**；合併後約 50 天人工跑 `gh workflow list` 確認仍為 active。網站的過期提示是停擺的警報。
- `snapshot_lines.py` 的 Odds API 只抓 `spreads`，大小分盤口目前只靠 ESPN。
- Legacy 流程仍在（P1 起 `preload` 不再排程，只剩手動 `workflow_dispatch`）：`public.games`、`cache_nba`、`sync_daily`、`settle_daily`、`train_base_model`、`train_calibrator`、
  `rebuild_features_from_boxscore`、`fill_b2b_nulls`、`preload_nba_data`、`export_legacy_csv`。不要擴充；要移除前先確認。
